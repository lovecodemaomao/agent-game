"""Deterministic official-protocol scenarios; no simulator or live LLM."""
import copy
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from coregeek_v2.world import WorldParser, Pos, freeze
from coregeek_v2.model import GameMemory, GameMemoryReducer, RuntimeState, Lifecycle, Signal, JobProposal, ReservationRequest
from coregeek_v2.runtime import Agent
from coregeek_v2.strategy import (DailyStrategy, layout, Context, EconomyPlanner, DefensePlanner, TaskPlanner,
                                  NightPrepPlanner, post_for, use_now)
from coregeek_v2.navigation import Navigation, distance
from coregeek_v2.rules import build_ring
from coregeek_v2.execution import TaskExecution
from coregeek_v2.jobs import FACTORIES, ProcurementJob, RebuildJob, MineTripJob, SellJob
from coregeek_v2.feedback import Outcome


def unit(rid,kind,x,y,health=None,level=1,backpack=()):
    return dict(id=rid,roleType=kind,pos=dict(x=x,y=y),health=health or (1500 if kind=='station' else 1000 if kind in ('rocket','railgun','wall') else 220),
                level=level,backpack=list(backpack),backPackCapability=40 if kind=='pioneer' else 100,cooldown=0)


def fixture(turn=1):
    return dict(roundNo=turn,mapInfo=dict(width=24,height=20,zones=[
        dict(neutralType=kind,pos=dict(x=x,y=y)) for kind,x,y in
        [('stone',2,15),('copper',10,8),('iron',9,4),('vendor',10,12),('weaponShop',8,15),('challengerTaskPoint1',4,16)]]),
        teamOur=dict(teamId='v2',type='challenger',goldNum=75,roles=[unit(10,'station',4,12),unit(1,'worker',3,12),
                        unit(2,'worker',6,12),unit(3,'pioneer',4,13)],playerTasks=[]),
        teamEnemy=dict(roles=[unit(20,'station',19,5)]),robot=dict(roles=[]),
        vendorShopList=[dict(name=k,price=v) for k,v in [('stone',1),('iron',3),('copper',5)]],
        weaponShopList=[dict(name=k,price=v) for k,v in [('WeaponUpgradeVoucher1',100),('WeaponUpgradeVoucher2',150),
          ('StationUpgradeVoucher1',100),('StationUpgradeVoucher2',150),('WallUpgradeVoucher1',20),('WallUpgradeVoucher2',30),
          ('WallFixer',10),('Amber',15)]],
        phaseTask='',lastRoundRoleActionResults={},lastSummonTreasureResult=0,llmResp='',lastCmdResult='',errors=[],worldNews={})


def armed(turn=1):
    raw=fixture(turn)
    weapons,_,posts=layout(WorldParser().parse(raw))
    raw['teamOur']['roles'] += [unit(30+i,k,p.x,p.y) for i,(k,p) in enumerate(weapons)]
    for rid,key in ((1,'rocket'),(2,'railgun'),(3,'support')):
        next(r for r in raw['teamOur']['roles'] if r['id']==rid)['pos']=posts[key].dump()
    return raw


def context(raw,memory=None):
    world=WorldParser().parse(raw)
    plan=DailyStrategy().plan(world,GameMemory(),None)
    return Context(world,freeze(memory or {}),plan,freeze({}),Navigation())


class DailyAndEconomyTests(unittest.TestCase):
    def test_day_one_layout_two_rockets_railgun_four_walls(self):
        c=context(fixture())
        self.assertEqual(Counter(k for k,p in c.plan.required_builds),{'rocket':2,'railgun':1,'wall':4})
        for kind,pos in c.plan.required_builds:
            self.assertEqual(build_ring(c.w,pos),2 if kind=='wall' else 1)
        rockets=[p for k,p in c.plan.required_builds if k=='rocket']
        self.assertTrue(all(distance(c.plan.posts['rocket'],p)==1 for p in rockets))

    def test_layout_mirrors_and_day_two_expands_half_ring(self):
        raw=fixture(131)
        raw['teamOur']['roles'][0]['pos']=dict(x=19,y=5)
        raw['teamEnemy']['roles'][0]['pos']=dict(x=4,y=12)
        c=context(raw)
        walls=[p for k,p in c.plan.required_builds if k=='wall']
        self.assertEqual(len(walls),10)
        self.assertTrue(all(build_ring(c.w,p)==2 for p in walls))
        self.assertTrue(all(p.x<=20 for p in walls))

    def test_day_plan_is_stable_during_day(self):
        raw=fixture()
        world=WorldParser().parse(raw)
        planner=DailyStrategy()
        first=planner.plan(world,GameMemory(),None)
        raw['roundNo']=30
        raw['teamOur']['goldNum']=900
        self.assertIs(planner.plan(WorldParser().parse(raw),GameMemory(),first),first)

    def test_all_posts_remain_accessible_after_half_ring_closes(self):
        for mirror in (False,True):
            raw=fixture(131)
            if mirror:
                raw['teamOur']['roles'][0]['pos']=dict(x=19,y=5)
                raw['teamEnemy']['roles'][0]['pos']=dict(x=4,y=12)
            world=WorldParser().parse(raw)
            weapons,walls,posts=layout(world)
            raw['teamOur']['roles']=[raw['teamOur']['roles'][0]]
            raw['teamOur']['roles'] += [unit(30+i,k,p.x,p.y) for i,(k,p) in enumerate(weapons)]
            raw['teamOur']['roles'] += [unit(50+i,'wall',p.x,p.y) for i,p in enumerate(walls)]
            world=WorldParser().parse(raw)
            for key,pos in posts.items():
                with self.subTest(mirror=mirror,post=key):
                    self.assertIsNotNone(Navigation().path(world,Pos(0,0),pos))

    def test_economy_distinct_mines_and_engineer_stone_quota(self):
        c=context(fixture())
        proposals=EconomyPlanner(c.nav).candidates(c)
        self.assertEqual(len({p.target for p in proposals if p}),len([p for p in proposals if p]))
        engineer=next(p for p in proposals if p and p.eligible_roles==(2,))
        self.assertEqual(engineer.data['ore'],'stone')
        self.assertEqual(engineer.data['quantity'],5)  # four walls + one spare

    def test_gold_target_does_not_stop_mining(self):
        raw=fixture()
        raw['teamOur']['goldNum']=999
        raw['teamOur']['roles'][2]['backpack']=['stone']*4
        c=context(raw)
        proposals=EconomyPlanner(c.nav).candidates(c)
        self.assertTrue(any(p and p.kind=='Mine' for p in proposals))

    def test_nearby_last_partial_mine_fits_tail(self):
        raw=armed(52)
        raw['teamOur']['roles'][2]['backpack']=['stone']*4
        c=context(raw)
        proposals=[p for p in EconomyPlanner(c.nav).candidates(c) if p]
        for p in proposals:
            job=FACTORIES[p.kind](p)
            self.assertLessEqual(job.remaining_duration(c.w,p.eligible_roles[0],p.target,c.nav),70-52+1)

    def test_explicit_future_price_news_can_hold_inventory(self):
        raw=armed()
        raw['teamOur']['goldNum']=500
        raw['teamOur']['roles'][1]['backpack']=['copper']*8
        c=context(raw,{'advice':{'hold_ores':[{'kind':'copper','until_day':2}]}})
        p=c.proposal('Sell','sell',c.w.role(1),Pos(10,12))
        self.assertEqual(SellJob(p).sellable(c.w,1),[])
        raw['teamOur']['goldNum']=190
        world=WorldParser().parse(raw)
        p=JobProposal('Sell','sell',(1,),'test',data=freeze({'gold_target':200,'hold_ores':('copper',),'post':Pos(6,12)}))
        self.assertEqual(SellJob(p).sellable(world,1),[('copper',2)])


class DeliveryAndDefenseTests(unittest.TestCase):
    def test_buy_does_not_force_full_health_wall_upgrade(self):
        raw=armed(261)
        raw['teamOur']['roles'].append(unit(50,'wall',7,12))
        raw['teamOur']['roles'][3]['backpack']=['WallUpgradeVoucher1']
        c=context(raw)
        p=c.proposal('Delivery','deliver',c.w.role(3),Pos(7,12),name='WallUpgradeVoucher1',target_id=50,hold=True)
        job=ProcurementJob(p)
        self.assertEqual(job.check(c.w,3,p.target),Signal.SUCCESS)
        self.assertEqual(job.stage,'HOLD')

    def test_damaged_base_upgrade_is_used_and_item_stays_world_owned(self):
        raw=armed(261)
        raw['teamOur']['roles'][0]['health']=600
        raw['teamOur']['roles'][3]['backpack']=['StationUpgradeVoucher1']
        c=context(raw)
        proposals=DefensePlanner(c.nav).candidates(c)
        self.assertTrue(any(p and p.data.get('name')=='StationUpgradeVoucher1' for p in proposals))
        self.assertTrue(use_now(c.w,c.w.station()))
        self.assertEqual(c.w.role(3).backpack,('StationUpgradeVoucher1',))

    def test_procurement_reserves_gold_and_uses_pioneer(self):
        raw=armed()
        raw['teamOur']['goldNum']=130
        c=context(raw)
        proposals=[p for p in DefensePlanner(c.nav).candidates(c) if p and p.kind=='Procure']
        self.assertEqual(proposals[0].eligible_roles,(3,))
        self.assertEqual(proposals[0].reservation.gold,100)
        self.assertEqual(c.w.role(proposals[0].data['target_id']).kind,'rocket')

    def test_regular_level_one_wall_rebuild_starts_with_remove(self):
        raw=armed()
        raw['teamOur']['roles'].append(unit(50,'wall',7,12,health=200))
        raw['teamOur']['roles'][2]['pos']=dict(x=6,y=11)
        raw['teamOur']['roles'][2]['backpack']=['stone']
        c=context(raw)
        p=c.proposal('Rebuild','wall',c.w.role(2),Pos(7,12),name='wall')
        self.assertEqual(RebuildJob(p).intent(c.w,2,p.target).action,'remove')

    def test_entire_night_keeps_defense_jobs_when_no_robot_nearby(self):
        raw=armed(71)
        agent=Agent.production(development=True)
        response=agent.respond(raw)
        self.assertFalse(response['roleCommandMap'])
        ids=next(iter(agent.sessions.values())).runtime.assignments.copy()
        raw['roundNo']=100
        response=agent.respond(raw)
        self.assertEqual(next(iter(agent.sessions.values())).runtime.assignments,ids)
        self.assertFalse(any(c['action']=='move' for c in response['roleCommandMap'].values()))

    def test_rocket_operator_obeys_cooldown_and_one_command(self):
        raw=armed(71)
        raw['teamOur']['roles'][4]['cooldown']=3
        raw['robot']['roles']=[dict(unit(80,'largeRobot',10,12,health=500),targetTeam='challenger')]
        response=Agent.production(development=True).respond(raw)
        controlled=[(rid,c) for rid,c in response['roleCommandMap'].items() if c.get('controllerId')=='1']
        self.assertEqual(len(controlled),1)
        self.assertEqual(controlled[0][0],'31')
        controllers=[c['controllerId'] for c in response['roleCommandMap'].values() if c['action']=='attack']
        self.assertEqual(len(controllers),len(set(controllers)))

    def test_night_support_uses_wall_fixer_without_reassigning_operator(self):
        raw=armed(71)
        pos=Pos.load(raw['teamOur']['roles'][3]['pos'])
        raw['teamOur']['roles'].append(unit(50,'wall',pos.x,pos.y+1,health=300,level=3))
        raw['teamOur']['roles'][3]['backpack']=['WallFixer']
        response=Agent.production(development=True).respond(raw)
        self.assertEqual(response['roleCommandMap']['3']['name'],'WallFixer')


class TaskAndNewsTests(unittest.TestCase):
    def task_setup(self):
        raw=armed()
        raw['teamOur']['roles'][3]['pos']=dict(x=4,y=15)
        raw['teamOur']['playerTasks']=[dict(taskPosition=dict(x=4,y=16),isValid=True,coldDownRounds=0,timeoutRounds=100,goldReward=30)]
        agent=Agent.production(development=True)
        agent.planners=(TaskPlanner(agent.navigation),)
        return raw,agent

    def test_task_probe_llm_command_answer_and_history(self):
        raw,agent=self.task_setup()
        first=agent.respond(raw)
        self.assertEqual(first['roleCommandMap']['3']['action'],'acceptTask')
        raw['phaseTask']='Read the task files and return the required answer.'
        raw['lastRoundRoleActionResults']={'3':True}
        for turn in (2,3,4):
            raw['roundNo']=turn
            raw['lastCmdResult']='[exitCode:0]\nprobe evidence'
            result=agent.respond(raw)
            self.assertTrue(result['executeCmd'])
            self.assertFalse(result['prompt'])
        raw['roundNo']=5
        result=agent.respond(raw)
        self.assertIn('probe evidence',result['prompt'])
        session=next(iter(agent.sessions.values()))
        job_id=session.runtime.assignments[3]
        request_id=session.runtime.jobs[job_id].behavior.pending['request_id']
        raw['roundNo']=6
        raw['llmResp']=json.dumps(dict(request_id=request_id,kind='command',command='cat /tmp/selfEvolutionTask/result.txt'))
        result=agent.respond(raw)
        self.assertEqual(result['executeCmd'],'cat /tmp/selfEvolutionTask/result.txt')
        raw['roundNo']=7
        raw['lastCmdResult']='[exitCode:0]\nanswer: 42'
        result=agent.respond(raw)
        self.assertIn('answer: 42',result['prompt'])
        request_id=next(iter(agent.sessions.values())).runtime.jobs[job_id].behavior.pending['request_id']
        raw['roundNo']=8
        raw['llmResp']=json.dumps(dict(request_id=request_id,kind='answer',answer='42'))
        result=agent.respond(raw)
        self.assertEqual(result['roleCommandMap']['3']['taskAnswer'],'42')
        self.assertFalse(result['prompt'] or result['executeCmd'])
        raw['roundNo']=9
        text=raw['phaseTask']
        raw['phaseTask']=''
        raw['teamOur']['playerTasks'][0]['isValid']=False
        result=agent.respond(raw)
        session=next(iter(agent.sessions.values()))
        self.assertEqual(session.runtime.jobs[job_id].lifecycle,Lifecycle.COMPLETED)
        self.assertEqual(session.memory.skills[text]['answer'],'42')

    def test_old_llm_response_cannot_execute_command(self):
        raw,agent=self.task_setup()
        raw['phaseTask']='Task text'
        for turn in (1,2,3,4):
            raw['roundNo']=turn
            result=agent.respond(raw)
        raw['roundNo']=5
        raw['llmResp']=json.dumps(dict(request_id='old-job:4:task',kind='command',command='WRONG'))
        result=agent.respond(raw)
        self.assertNotEqual(result['executeCmd'],'WRONG')

    def test_news_quota_and_source_validation(self):
        memory=GameMemory(news=[freeze({'officialNews':'Copper stops tomorrow.'})])
        state=RuntimeState()
        execution=TaskExecution()
        count=0
        for turn in range(1,6):
            world=WorldParser().parse(fixture(turn))
            execution.receive(world,state,memory)
            prompt,cmd=execution.output(world,state,memory)
            count+=bool(prompt)
            self.assertFalse(cmd)
        self.assertEqual(count,3)
        GameMemoryReducer().accept_advice(memory,world,dict(blocked_mines=[dict(kind='copper',start_day=2,end_day=2,evidence='invented')]))
        self.assertFalse(memory.advice['blocked_mines'])
        GameMemoryReducer().accept_advice(memory,world,dict(blocked_mines=[dict(kind='copper',start_day=2,end_day=2,evidence='Copper stops tomorrow.')]))
        self.assertEqual(memory.advice['blocked_mines'][0]['kind'],'copper')

    def test_treasure_result_not_inventory_guess(self):
        raw=armed()
        c=context(raw)
        p=c.proposal('Treasure','treasure',c.w.role(3),Pos(4,14),items=('Amber',),start=1,end=70)
        job=FACTORIES['Treasure'](p)
        signal=job.on_outcome(Outcome(1,3,'job','SUMMON','TREASURE_WRONG_ITEMS',freeze({})))
        self.assertEqual(signal,Signal.FAIL)
        self.assertTrue(job.attempted)

    def test_no_source_imports_old_agent_or_executes_shell(self):
        import ast
        root=Path(__file__).resolve().parents[1]/'src/coregeek_v2'
        for file in root.glob('*.py'):
            tree=ast.parse(file.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith('agent'),file.name)
                if isinstance(node,ast.Import):
                    self.assertFalse(any(n.name in ('subprocess','requests','urllib.request') for n in node.names),file.name)


if __name__=='__main__':
    unittest.main()
