"""Persistent procurement and complete mine -> vendor -> home route scoring."""
from collections import Counter
from .geography import INF
from .protocol import distance

ORES=('stone','iron','copper')
SUMMON_ORDER='LargeRobotSummonOrder'      # 大机器人召唤令(需求: 第一天金钱>100优先购买)
SUMMON_ORDER_TRIGGER=100
RETURN_MARGIN=5                           # 与 brain.RETURN_MARGIN 保持一致
HARVEST_BUFFER=2                          # 顺手采集只留 2 回合缓冲（返程已在 margin 内）
NEAR_HOME_RADIUS=4                        # "家附近"判定半径（基地切比雪夫距离）
HARVEST_DETOUR=2                          # 顺路判据: 多绕不超过 2 回合即视为顺手
NIGHT_OVERRUN=12                          # 首日召唤令专差允许延伸入夜的回合数
HP={'station':(1500,3000,4500),'wall':(1000,1500,2000),
    'rocket':(1000,1500,2000),'railgun':(1000,1500,2000),'gatling':(1000,1500,2000)}


class Economy:
    def __init__(self,p):
        self.p=p; self.turn=p.turn; self.m=p.memory
        self.mine_claims=Counter()

    def options(self):
        """升级/修复候选，按天数调整优先级（需求3）:

        - 第1天: 武器升级优先（尽量当天把武器升上去）
        - 第2天起: 先把围墙升到 2 级（迎敌半圈），随后若还有余量时间与金钱，
          再回头升武器；围墙 2->3 级排在武器之后
        - 与天数无关的保命项始终最优先: 濒危基地(<60%血)、三级残血墙的 WallFixer
        """
        options=[]
        # 天数兜底: memory.day 未初始化时按回合号推导，保证优先级判断稳定
        day=self.m.day or ((self.turn.round_no-1)//130+1)
        weapon_first=day<=1
        # 围墙阶段: 迎敌半圈的主要墙体（≥一半）升到2级前，围墙券优先于武器券；
        # 达标后武器升级接管（需求3: 围墙升级成功后若还有余量，再执行武器升级）。
        # 若不设这个"阶段完成"判据，10 座墙的券会一直插队，武器永远升不上去。
        all_walls=self.turn.walls()
        wall_done=sum(1 for w in all_walls if max(1,min(3,w.level))>=2)
        wall_phase=bool(all_walls) and wall_done < max(1,len(all_walls)//2)
        for u in (*self.turn.weapons(), *self.turn.walls(), self.turn.station()):
            if u is None: continue
            level=max(1,min(3,u.level)); ratio=u.health/HP[u.kind][level-1]
            if u.kind=='wall' and level==3:
                if ratio>=0.7: continue
                item='WallFixer'; priority=0 if ratio<0.35 else 4
            elif u.kind=='station' and ratio<0.6 and level<3:
                item=f'StationUpgradeVoucher{level}'; priority=0
            else:
                if level>=3: continue
                prefix='Station' if u.kind=='station' else 'Wall' if u.kind=='wall' else 'Weapon'
                item=f'{prefix}UpgradeVoucher{level}'
                if u.kind=='wall':
                    # 围墙1->2级: 第1天让位武器; 第2天起在围墙阶段优先, 阶段完成后让位武器
                    priority = (2 if level==1 else 2.5) if weapon_first else \
                               (1 if (level==1 and wall_phase) else 3)
                elif u.kind=='rocket':
                    priority=1 if weapon_first else (3 if wall_phase else 1)
                else:                      # railgun / gatling
                    priority=1.5 if weapon_first else (3.5 if wall_phase else 1.5)
            # 围墙: 越朝向机器人越优先（self.p.walls 已按迎敌方向由前到后排序），
            # 非迎敌半圈的墙排到最后；受损墙再略微提前（ratio 越小越靠前）。
            if u.kind=='wall':
                if u.pos in self.p.walls:
                    priority += self.p.walls.index(u.pos)*0.05 + ratio*0.1
                else:
                    priority += 2 + ratio*0.1
            options.append((priority,level,u.unit_id,item,u))
        return sorted(options,key=lambda x:x[:3])

    def target(self,job):
        return next((u for u in self.turn.ours if u.unit_id==job.get('unit') and u.health>0),None)

    def usable(self,job,target):
        if target is None: return False
        if job['item']=='WallFixer': return target.health<HP['wall'][max(1,target.level)-1]
        return target.level==job['level']

    def prepare(self):
        for role in self.turn.workers():
            job=self.m.jobs.get(role.unit_id)
            if job and job.get('type')=='upgrade':
                if not self.usable(job,self.target(job)):
                    self.m.jobs.pop(role.unit_id,None)
                else:
                    job['bought']=job['item'] in role.backpack
                    if not job['bought'] and (not self.turn.is_day or self.p.remaining<8):
                        self.m.jobs.pop(role.unit_id,None)
        occupied={j['unit'] for j in self.m.jobs.values() if j.get('type')=='upgrade'}
        # Recover vouchers already held, including after a worker was revived.
        for role in self.turn.workers():
            if self.m.jobs.get(role.unit_id,{}).get('type')=='upgrade': continue
            for _,level,uid,item,target in self.options():
                if uid not in occupied and item in role.backpack:
                    self.m.jobs[role.unit_id]={'type':'upgrade','unit':uid,'item':item,
                        'level':level,'bought':True,'price':0}
                    occupied.add(uid)
                    break
        if not self.turn.is_day or len(self.turn.weapons())<3: return
        # 需求2: 第一天在买升级券之前，只要金钱超过100 就优先买大机器人召唤令
        if (self.m.day<=1 and not self.m.summon_order_done
                and SUMMON_ORDER in self.p.shop_prices
                and self.p.gold>SUMMON_ORDER_TRIGGER
                and not any(j.get('type')=='order' for j in self.m.jobs.values())
                and not any(SUMMON_ORDER in r.backpack for r in self.turn.controllable())):
            # 首日破百通常在白昼后段, 商店往返来不及但值得为 100 金骚扰道具
            # 延伸入夜（首夜仅少量小型机器人, 且召唤令买到即用、无需带回）
            courier=self._courier_for(self.p.shop_prices[SUMMON_ORDER],
                                      extra_slack=NIGHT_OVERRUN)
            if courier:
                role,shop=courier
                self.m.jobs[role.unit_id]={'type':'order','item':SUMMON_ORDER,
                    'price':self.p.shop_prices[SUMMON_ORDER],'shop':shop}
                self.m.event(f'worker {role.unit_id}: day-1 priority buy {SUMMON_ORDER}')
                return
        # One purchasing courier at a time; the other worker keeps producing money.
        if any(j.get('type') in ('upgrade','order') for j in self.m.jobs.values()): return
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        for priority,level,uid,item,target in self.options():
            price=self.p.shop_prices.get(item)
            if price is None or price>self.p.gold: continue
            # 第一天: 围墙是"用石头现场建"的，不得用采购额度抢武器升级的预算；
            # 武器升级券与保命项照常允许（需求3: 武器升级尽量在第一天完成）。
            day=self.m.day or ((self.turn.round_no-1)//130+1)
            if day==1 and self.p.missing_walls() and not item.startswith('Weapon'):
                continue
            choices=[]
            for role in self.turn.workers():
                if role.backpack_full: continue
                routes=self.p.route(role)
                for shop in shops:
                    stand=routes.adjacent(shop)
                    if stand is None: continue
                    delivery=self.p.geo.to(target.pos).get(stand,INF)
                    return_cost=max((self.p.home_cost(role,s) for s in self.p.geo.seats(target.pos)),default=INF)
                    cost=routes.cost[stand]+delivery+return_cost+2+5
                    if cost<self.p.remaining:
                        choices.append((cost,role,shop))
            if choices:
                _,role,shop=min(choices,key=lambda x:x[0])
                self.m.jobs[role.unit_id]={'type':'upgrade','unit':uid,'item':item,
                    'level':level,'bought':False,'price':price,'shop':shop}
                self.m.event(f'worker {role.unit_id}: purchase {item} for building {uid}')
                return

    def _courier_for(self,price,extra_slack=0):
        """挑一名能负担"去商店->再回防"整段时间的工人做采购。

        extra_slack: 允许的额外回合余量（首日召唤令专差可延伸入夜, 见 NIGHT_OVERRUN）。
        """
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        best=None
        for role in self.turn.workers():
            if role.backpack_full: continue
            routes=self.p.route(role)
            for shop in shops:
                stand=routes.adjacent(shop)
                if stand is None: continue
                cost=routes.cost[stand]+self.p.home_cost(role,stand)+2+RETURN_MARGIN
                if cost<self.p.remaining+extra_slack and (best is None or cost<best[0]):
                    best=(cost,role,shop)
        return (best[1],best[2]) if best else None

    def order(self,role,routes):
        """召唤令采购/使用: 买到即用（使用不需要目标位置）。"""
        job=self.m.jobs.get(role.unit_id)
        if not job or job.get('type')!='order': return False
        item=job['item']
        if item in role.backpack:
            self.p.commands[str(role.unit_id)]={'action':'use','name':item}
            self.m.jobs.pop(role.unit_id,None)
            self.m.summon_order_done=True
            self.m.event(f'worker {role.unit_id}: use {item}')
            return True
        if not self.turn.is_day or job['price']>self.p.gold:
            self.m.jobs.pop(role.unit_id,None)
            return False
        shop=job['shop']
        if routes.distance(shop)>=INF:
            self.m.jobs.pop(role.unit_id,None)
            return False
        if self.p.interact(role,routes,shop,'buy',name=item,num=1):
            if routes.distance(shop)==0: self.p.gold-=job['price']
            return True
        return False

    def act_urgent(self,role,routes):
        """入夜前也必须完成的事: 召唤令采购/使用 + 升级券送达。"""
        return self.order(role,routes) or self.upgrade(role,routes)

    def harvest_near_home(self,role,routes):
        """需求3: 白天回防途中在家附近顺手采矿。

        只做"不耽误入夜前回到武器旁"的顺路采集：矿点必须在基地附近，
        且 走到矿点 + 采一次 + 从矿点回位 + 返程余量 仍在白昼剩余回合内。
        """
        if role.backpack_full: return False
        station=self.turn.station()
        if station is None: return False
        best=None
        home_now=self.p.home_cost(role,role.pos)
        for mine,kind in self.turn.zones.items():
            price=self.p.prices.get(kind,0)
            if kind not in ORES or price<=0 or self.blocked(mine,kind): continue
            for seat in self.p.geo.seats(mine):
                travel=routes.cost.get(seat,INF)
                if travel>=INF: continue
                home=self.p.home_cost(role,seat)
                # "顺手"判据: 家附近 或 相对当前返程路线只多绕 HARVEST_DETOUR 回合内
                # （矿点不会生成在基地建造区内, 单看半径往往永远不中）
                detour=travel+1+home-home_now
                if distance(mine,station.pos)>NEAR_HOME_RADIUS and detour>HARVEST_DETOUR:
                    continue
                if travel+1+home+HARVEST_BUFFER<=self.p.remaining:
                    if best is None or travel<best[0]:
                        best=(travel,mine,seat)
        if best is None: return False
        travel,mine,seat=best
        if role.pos==seat:
            self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[mine.dump()]}
            self.m.event(f'worker {role.unit_id}: harvest {self.turn.zones[mine]} near home on the way back')
            return True
        return self.p.move(role,routes,seat)

    def upgrade(self,role,routes):
        job=self.m.jobs.get(role.unit_id)
        if not job or job.get('type')!='upgrade': return False
        target=self.target(job)
        if not self.usable(job,target):
            self.m.jobs.pop(role.unit_id,None)
            return False
        if job['item'] in role.backpack:
            if routes.distance(target.pos)==0:
                self.p.interact(role,routes,target.pos,'use',name=job['item'],targetPos=[target.pos.dump()])
                self.m.event(f'worker {role.unit_id}: use {job["item"]} on {target.unit_id}')
                return True
            if self.turn.is_day and self.p.enough_time(role,routes,target.pos):
                return self.p.interact(role,routes,target.pos,'use',name=job['item'],targetPos=[target.pos.dump()])
            return False
        if not self.turn.is_day: return False
        if self.p.remaining<=self.p.home_cost(role,role.pos)+5:
            self.m.jobs.pop(role.unit_id,None)
            return False
        shop=job['shop']
        if job['price']>self.p.gold or role.backpack_full or routes.distance(shop)>=INF:
            self.m.jobs.pop(role.unit_id,None)
            return False
        if self.p.interact(role,routes,shop,'buy',name=job['item'],num=1):
            if routes.distance(shop)==0: self.p.gold-=job['price']
            return True
        return False

    def blocked(self,pos,kind):
        if self.m.mine_blocked_until.get(pos,0)>self.turn.round_no: return True
        return any(e['kind']==kind and e['start_day']<=self.m.day<=e['end_day']
                   for e in self.m.news_advice.get('blocked_mines',[]))

    # ---------------------------------------------------------------- 矿工分工
    def stone_needed(self):
        """是否还需要专人采石: 建墙缺格 或 在途石头不足以补齐。"""
        missing=len(self.p.missing_walls())
        if missing<=0: return False
        stock=sum(r.backpack.count('stone') for r in self.turn.workers())
        return stock<missing

    def family(self,role):
        """矿工分工: 固定一名采石工供建墙材料，其余工采矿(铁/铜)。

        避免两人都去采石导致矿石收入为零（效率过低）。
        墙建齐或石头足够时全员转为采矿。
        """
        workers=sorted(self.turn.workers(),key=lambda r:r.unit_id)
        ids=[r.unit_id for r in workers]
        if not ids: return 'ore'
        if not self.stone_needed():
            self.m.mining_roles={}
            return 'ore'
        stone_id=self.m.mining_roles.get('stone')
        if stone_id not in ids:
            # 固定取 unit_id 最小者采石，其余采矿（不随回合漂移）
            stone_id=ids[0]
            self.m.mining_roles={'stone':stone_id}
            self.m.event(f'worker {stone_id}: assigned to stone, others to ore')
        return 'stone' if role.unit_id==stone_id else 'ore'

    def wanted_kinds(self,role):
        fam=self.family(role)
        return ('stone',) if fam=='stone' else ('iron','copper')

    def sale_candidates(self,role,routes,extra=None):
        stock=Counter(x for x in role.backpack if x in ORES)
        if extra: stock.update(extra)
        if not stock: return []
        value=sum(n*self.p.prices.get(k,0) for k,n in stock.items())
        candidates=[]
        for vendor,k in self.turn.zones.items():
            if k!='vendor': continue
            for seat in self.p.geo.seats(vendor):
                walk=routes.cost.get(seat,INF)
                home=self.p.home_cost(role,seat)
                total=walk+len(stock)+home+5
                if total<self.p.remaining:
                    candidates.append({'score':value/max(1,walk+len(stock)),
                        'vendor':vendor,'seat':seat,'total':total,'stock':stock,'value':value})
        return candidates

    def mining_candidates(self,role,routes,kinds=None):
        capacity=max(0,(role.capacity or 100)-len(role.backpack))
        if not capacity: return []
        stock=Counter(x for x in role.backpack if x in ORES)
        inventory_value=sum(n*self.p.prices.get(k,0) for k,n in stock.items())
        candidates=[]
        vendors=[q for q,k in self.turn.zones.items() if k=='vendor']
        for mine,kind in self.turn.zones.items():
            price=self.p.prices.get(kind,0)
            if kind not in ORES or price<=0 or self.blocked(mine,kind): continue
            if kinds is not None and kind not in kinds: continue
            quantity=min(capacity,max(1,10-self.m.mine_used.get(mine,0)-self.mine_claims[mine]))
            sales=len(set(stock)|{kind})
            for entry in self.p.geo.seats(mine):
                approach=routes.cost.get(entry,INF)
                if approach>=INF: continue
                # Consider each actual vendor seat: obstacles and the final return
                # are included together, not independently minimized legs.
                for vendor in vendors:
                    for exit in self.p.geo.seats(vendor):
                        trip=self.p.geo.field([exit]).get(entry,INF)
                        home=self.p.home_cost(role,exit)
                        max_q=min(quantity,self.p.remaining-approach-trip-sales-home-6)
                        if max_q<1: continue
                        for q in range(1,int(max_q)+1):
                            time=approach+q+trip+sales
                            score=(inventory_value+price*q)/time
                            candidates.append({'score':score,'target':mine,'kind':kind,
                                'entry':entry,'vendor':vendor,'exit':exit,'left':q,
                                'total':time+home+5,'price':price,'type':'mine'})
        return candidates

    def sell(self,role,routes,job):
        stock=Counter(x for x in role.backpack if x in ORES)
        if not stock:
            self.m.jobs.pop(role.unit_id,None)
            return False
        vendor=job['vendor']
        seat=job.get('exit',job.get('seat'))
        if seat not in routes.cost:
            seat=routes.adjacent(vendor)
        if seat is None: return False
        if self.p.remaining <= routes.cost[seat]+len(stock)+self.p.home_cost(role,seat)+1:
            return False
        if distance(role.pos,vendor)<=1:
            name=max(stock,key=lambda k:stock[k]*self.p.prices.get(k,0))
            self.p.commands[str(role.unit_id)]={'action':'sell','name':name,'num':stock[name]}
            return True
        return self.p.move(role,routes,seat)

    def act(self,role,routes):
        if self.order(role,routes): return True
        if self.upgrade(role,routes): return True
        job=self.m.jobs.get(role.unit_id)
        if job and job.get('type')=='sell':
            if self.sell(role,routes,job): return True
            self.m.jobs.pop(role.unit_id,None)
        if job and job.get('type')=='mine':
            invalid=(self.turn.zones.get(job['target'])!=job['kind'] or
                     self.blocked(job['target'],job['kind']) or role.backpack_full)
            changed_price=self.p.prices.get(job['kind'])!=job['price']
            if not invalid and not changed_price and job['left']>0:
                entry=job['entry']; exit=job['exit']
                cost=(routes.cost.get(entry,INF)+job['left']+
                      self.p.geo.field([exit]).get(entry,INF)+3+self.p.home_cost(role,exit)+5)
                if cost<self.p.remaining:
                    self.mine_claims[job['target']]+=job['left']
                    if role.pos==entry:
                        self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[job['target'].dump()]}
                        return True
                    if self.p.move(role,routes,entry): return True
            if job['left']<=0 and not changed_price:
                sale={'type':'sell','vendor':job['vendor'],'exit':job['exit']}
                self.m.jobs[role.unit_id]=sale
                if self.sell(role,routes,sale): return True
            self.m.jobs.pop(role.unit_id,None)
        sales=self.sale_candidates(role,routes)
        # 分工采集: 采石工只去石矿, 其余工只去铁矿/铜矿; 本工种无可用矿时回退到全部矿种
        kinds=self.wanted_kinds(role)
        mines=self.mining_candidates(role,routes,kinds)
        if not mines:
            mines=self.mining_candidates(role,routes)
        sale=max(sales,key=lambda x:x['score'],default=None)
        mine=max(mines,key=lambda x:x['score'],default=None)
        value=sum(self.p.prices.get(k,0) for k in role.backpack if k in ORES)
        funding=next((self.p.shop_prices[item] for _,_,_,item,_ in self.options()
                      if item in self.p.shop_prices and self.p.shop_prices[item]>self.p.gold),None)
        unlock=funding is not None and self.p.gold+value>=funding
        if sale and (mine is None or sale['score']>=mine['score'] or unlock):
            self.m.jobs[role.unit_id]={'type':'sell',**sale}
            return self.sell(role,routes,self.m.jobs[role.unit_id])
        if mine:
            self.m.jobs[role.unit_id]=mine
            self.mine_claims[mine['target']]+=mine['left']
            if role.pos==mine['entry']:
                self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[mine['target'].dump()]}
                return True
            return self.p.move(role,routes,mine['entry'])
        return False
