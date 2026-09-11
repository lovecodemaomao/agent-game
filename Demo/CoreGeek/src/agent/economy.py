"""Persistent procurement and complete mine -> vendor -> home route scoring."""
from collections import Counter
from .geography import INF
from .protocol import distance

ORES=('stone','iron','copper')
HP={'station':(1500,3000,4500),'wall':(1000,1500,2000),
    'rocket':(1000,1500,2000),'railgun':(1000,1500,2000),'gatling':(1000,1500,2000)}


class Economy:
    def __init__(self,p):
        self.p=p; self.turn=p.turn; self.m=p.memory
        self.mine_claims=Counter()

    def options(self):
        options=[]
        for u in (*self.turn.weapons(), *self.turn.walls(), self.turn.station()):
            if u is None: continue
            level=max(1,min(3,u.level)); ratio=u.health/HP[u.kind][level-1]
            if u.kind=='wall' and level==3:
                if ratio>=0.7: continue
                item='WallFixer'; priority=0 if ratio<0.35 else 4
            else:
                if level>=3: continue
                prefix='Station' if u.kind=='station' else 'Wall' if u.kind=='wall' else 'Weapon'
                item=f'{prefix}UpgradeVoucher{level}'
                if u.kind=='station' and ratio<0.6: priority=0
                elif u.kind=='wall' and ratio<0.5: priority=0.5
                elif u.kind=='rocket' and level==1: priority=1
                elif u.kind=='wall' and level==1: priority=2
                elif u.kind=='rocket': priority=3
                elif u.kind=='railgun': priority=4
                elif u.kind=='wall': priority=5
                else: priority=4
            # Favor forward, damaged walls rather than uniformly buying every wall.
            priority += (0 if u.kind!='wall' else (0 if u.pos in self.p.walls else 2)+ratio*0.1)
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
        # One purchasing courier at a time; the other worker keeps producing money.
        if any(j.get('type')=='upgrade' for j in self.m.jobs.values()): return
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        for priority,level,uid,item,target in self.options():
            price=self.p.shop_prices.get(item)
            if price is None or price>self.p.gold: continue
            if self.m.day==1 and self.p.missing_walls() and priority>=1: continue
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

    def mining_candidates(self,role,routes):
        capacity=max(0,(role.capacity or 100)-len(role.backpack))
        if not capacity: return []
        stock=Counter(x for x in role.backpack if x in ORES)
        inventory_value=sum(n*self.p.prices.get(k,0) for k,n in stock.items())
        candidates=[]
        vendors=[q for q,k in self.turn.zones.items() if k=='vendor']
        for mine,kind in self.turn.zones.items():
            price=self.p.prices.get(kind,0)
            if kind not in ORES or price<=0 or self.blocked(mine,kind): continue
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
