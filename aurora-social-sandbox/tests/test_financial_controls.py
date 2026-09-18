import os, tempfile
fd, path = tempfile.mkstemp(suffix='.db')
os.close(fd)
os.environ['DATABASE_URL'] = f'sqlite:///{path}'
os.environ['ADMIN_KEY'] = 'test-admin'

from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import app
from app.db import SessionLocal
from app.models import User, Wallet, JournalEntry, ExposureState

client = TestClient(app)

def reset_player(gc=100, sc=20, access=True, excluded=False):
    with SessionLocal() as db:
        u=db.get(User,1); u.sandbox_access=access; u.is_self_excluded=excluded
        for cur,bal in [('GC',gc),('SC',sc)]:
            w=db.scalar(select(Wallet).where(Wallet.user_id==1, Wallet.currency==cur)); w.balance=bal
        e=db.get(ExposureState,1); e.reserved_sc=0; e.max_sc=100_000
        db.commit()

def test_01_health():
    assert client.get('/api/health').json()['live_payments'] is False

def test_02_games_publish_math():
    games=client.get('/api/games').json(); assert len(games)==3 and all('rtp' in g for g in games)

def test_03_server_prices_purchase():
    reset_player()
    r=client.post('/api/sandbox/purchase',json={'request_id':'purchase-a1','user_id':1,'package_key':'starter'})
    assert r.status_code==200 and r.json()['server_price']['usd_cents']==499

def test_04_purchase_idempotent():
    reset_player(gc=0,sc=0)
    body={'request_id':'purchase-idem-1','user_id':1,'package_key':'starter'}
    a=client.post('/api/sandbox/purchase',json=body).json(); b=client.post('/api/sandbox/purchase',json=body).json()
    assert a['wallets']==b['wallets'] and b['replayed'] is True

def test_05_purchase_id_conflict():
    reset_player()
    client.post('/api/sandbox/purchase',json={'request_id':'purchase-conflict','user_id':1,'package_key':'starter'})
    r=client.post('/api/sandbox/purchase',json={'request_id':'purchase-conflict','user_id':1,'package_key':'popular'})
    assert r.status_code==409

def test_06_insufficient_balance_blocks_play():
    reset_player(gc=0,sc=0)
    r=client.post('/api/play',json={'request_id':'round-low-bal','user_id':1,'game_key':'prism-flip','currency':'GC','stake':1})
    assert r.status_code==409

def test_07_round_idempotent_no_reroll():
    reset_player(gc=1000,sc=20)
    body={'request_id':'round-idem-1','user_id':1,'game_key':'prism-flip','currency':'GC','stake':10}
    a=client.post('/api/play',json=body).json(); b=client.post('/api/play',json=body).json()
    assert a['result']==b['result'] and a['wallets']==b['wallets'] and b['replayed'] is True

def test_08_round_id_conflict():
    reset_player(gc=1000,sc=20)
    client.post('/api/play',json={'request_id':'round-conflict','user_id':1,'game_key':'prism-flip','currency':'GC','stake':10})
    r=client.post('/api/play',json={'request_id':'round-conflict','user_id':1,'game_key':'nova-roll','currency':'GC','stake':10})
    assert r.status_code==409

def test_09_gc_sc_separate():
    reset_player(gc=100,sc=20)
    before=client.get('/api/player/1').json()['wallets']
    client.post('/api/play',json={'request_id':'round-separate','user_id':1,'game_key':'prism-flip','currency':'GC','stake':5})
    after=client.get('/api/player/1').json()['wallets']
    assert before['SC']==after['SC']

def test_10_self_exclusion_blocks_play():
    reset_player(excluded=True)
    r=client.post('/api/play',json={'request_id':'round-excluded','user_id':1,'game_key':'prism-flip','currency':'GC','stake':1})
    assert r.status_code==403

def test_11_access_gate_blocks_play():
    reset_player(access=False)
    r=client.post('/api/play',json={'request_id':'round-noaccess','user_id':1,'game_key':'prism-flip','currency':'GC','stake':1})
    assert r.status_code==403

def test_12_redemption_reserves_sc():
    reset_player(sc=10)
    r=client.post('/api/redemptions',json={'request_id':'redeem-1a','user_id':1,'amount':4})
    assert r.status_code==200 and r.json()['wallets']['SC']==6 and r.json()['status']=='pending'

def test_13_redemption_idempotent():
    reset_player(sc=10)
    body={'request_id':'redeem-idem','user_id':1,'amount':3}
    a=client.post('/api/redemptions',json=body).json(); b=client.post('/api/redemptions',json=body).json()
    assert a['wallets']==b['wallets'] and b['replayed'] is True

def test_14_redemption_cannot_overdraw():
    reset_player(sc=2)
    r=client.post('/api/redemptions',json={'request_id':'redeem-too-much','user_id':1,'amount':3})
    assert r.status_code==409

def test_15_admin_requires_auth():
    assert client.get('/api/admin/treasury').status_code==401

def test_16_admin_credit_idempotent_and_audited():
    reset_player(sc=0)
    body={'request_id':'credit-idem','user_id':1,'currency':'SC','amount':50}
    h={'X-Admin-Key':'test-admin'}
    a=client.post('/api/admin/test-credit',json=body,headers=h).json(); b=client.post('/api/admin/test-credit',json=body,headers=h).json()
    assert a['created'] is True and b['created'] is False and b['wallets']['SC']==50
    audit=client.get('/api/admin/audit',headers=h).json(); assert any(x['action']=='test_credit' for x in audit)

def test_17_exposure_budget_blocks_unfunded_round():
    reset_player(sc=1000)
    with SessionLocal() as db:
        e=db.get(ExposureState,1); e.max_sc=10; db.commit()
    r=client.post('/api/play',json={'request_id':'round-exposure','user_id':1,'game_key':'comet-24','currency':'SC','stake':1})
    assert r.status_code==503

def test_18_exposure_released_after_round():
    reset_player(sc=1000)
    r=client.post('/api/play',json={'request_id':'round-release','user_id':1,'game_key':'nova-roll','currency':'SC','stake':1})
    assert r.status_code==200
    t=client.get('/api/admin/treasury',headers={'X-Admin-Key':'test-admin'}).json(); assert t['risk']['SC_reserved_open_rounds']==0

def test_19_every_journal_group_balances():
    reset_player(gc=1000,sc=100)
    client.post('/api/play',json={'request_id':'round-balance','user_id':1,'game_key':'prism-flip','currency':'GC','stake':10})
    with SessionLocal() as db:
        rows=db.execute(select(JournalEntry.txn_id,JournalEntry.currency,func.sum(JournalEntry.amount)).group_by(JournalEntry.txn_id,JournalEntry.currency)).all()
        assert rows and all(total==0 for _,_,total in rows)

def test_20_operator_has_no_outcome_override_route():
    schema=client.get('/openapi.json').json()
    paths=' '.join(schema['paths'].keys())
    assert 'force-win' not in paths and 'set-outcome' not in paths and 'winner' not in paths
