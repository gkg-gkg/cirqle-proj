"""Phase C — the referral tree and network quality.

The tree walks user-supplied structure, so most of what matters here is that it
cannot be made to loop, explode or leak. The rest is about the one figure a
merchant makes decisions on: whether a referrer's people actually come back.
"""
from datetime import date, timedelta

from sqlmodel import Session

from app.customers import (MAX_TREE_CHILDREN, MAX_TREE_DEPTH, build_customers,
                           network_depth, network_summary, referral_tree,
                           referrer_scores)
from app.models import (Campaign, Merchant, Receipt, ReferralReward, User)

TODAY = date(2026, 9, 8)


def _merchant(session: Session, email="shop@example.com") -> Merchant:
    m = Merchant(email=email, password_hash="x", business_name="Shop")
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _campaign(session: Session, m: Merchant) -> Campaign:
    c = Campaign(brand="Shop", card_title="Deal", merchant_id=m.id)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _user(session: Session, n: int) -> User:
    u = User(first_name="M", last_name=str(n), email=f"m{n}@x.com",
             password_hash="x", instagram_handle=f"member{n}", status="approved")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _buy(session: Session, user: User, c: Campaign, day: date, *,
         total=20.0, referred_by: User = None, tag=""):
    r = Receipt(user_id=user.id, post_id=f"p{user.id}-{day}-{tag}",
                campaign_id=c.id, brand=c.brand, amount=1.0, image_key="k",
                status="confirmed", basket_total=total, purchase_date=day)
    if referred_by is not None:
        r.referred_by_user_id = referred_by.id
        r.referred_by_handle = referred_by.instagram_handle
    session.add(r)
    session.commit()
    return r


def _chain(session, c, n, *, spend=20.0):
    """n users where each was referred by the one before. Returns them in order."""
    users = []
    for i in range(n):
        u = _user(session, i + 1)
        _buy(session, u, c, TODAY - timedelta(days=200 - i * 10), total=spend,
             referred_by=users[-1] if users else None)
        users.append(u)
    return users


# ── Shape of the tree ────────────────────────────────────────────────────────

def test_tree_nests_each_generation(session):
    m = _merchant(session)
    c = _campaign(session, m)
    a, b, d = _chain(session, c, 3)

    tree = referral_tree(build_customers(m.id, session), a.id)

    assert tree["handle"] == "member1"
    assert [n["handle"] for n in tree["referred"]] == ["member2"]
    assert [n["handle"] for n in tree["referred"][0]["referred"]] == ["member3"]


def test_rollup_counts_the_whole_subtree(session):
    """The number that makes the tree worth opening: not "referred one person"
    but "this member is worth £60 to you"."""
    m = _merchant(session)
    c = _campaign(session, m)
    a, b, d = _chain(session, c, 3, spend=20.0)

    tree = referral_tree(build_customers(m.id, session), a.id)

    assert tree["peopleBelow"] == 2            # b and d
    assert tree["spendBelow"] == 40.0          # their spend, not a's own
    assert tree["referred"][0]["peopleBelow"] == 1
    assert tree["referred"][0]["spendBelow"] == 20.0


def test_leaf_has_empty_rollup(session):
    m = _merchant(session)
    c = _campaign(session, m)
    (solo,) = _chain(session, c, 1)

    tree = referral_tree(build_customers(m.id, session), solo.id)

    assert tree["referred"] == []
    assert tree["peopleBelow"] == 0
    assert tree["spendBelow"] == 0.0


def test_unknown_customer_is_none(session):
    m = _merchant(session)
    _campaign(session, m)
    assert referral_tree(build_customers(m.id, session), 999) is None


# ── The things that could break it ───────────────────────────────────────────

def test_a_referral_loop_does_not_hang(session):
    """A refers B, then B refers A back on a second post. Legal in the data —
    a claim is unique on (user, post), not on the pair of people — so the walk
    has to survive it."""
    m = _merchant(session)
    c = _campaign(session, m)
    a, b = _user(session, 1), _user(session, 2)
    _buy(session, a, c, TODAY - timedelta(days=100), tag="a1")
    _buy(session, b, c, TODAY - timedelta(days=90), referred_by=a, tag="b1")
    # B now refers A, using a different post.
    a2 = Receipt(user_id=a.id, post_id="a-second", campaign_id=c.id, amount=1.0,
                 image_key="k", status="confirmed", basket_total=20.0,
                 purchase_date=TODAY - timedelta(days=80),
                 referred_by_user_id=b.id, referred_by_handle="member2")
    session.add(a2)
    session.commit()

    tree = referral_tree(build_customers(m.id, session), a.id)

    assert tree is not None                     # terminated at all
    assert tree["peopleBelow"] <= 1             # A is not counted below itself


def test_depth_is_capped(session):
    m = _merchant(session)
    c = _campaign(session, m)
    users = _chain(session, c, MAX_TREE_DEPTH + 4)

    tree = referral_tree(build_customers(m.id, session), users[0].id)

    depth = 0
    node = tree
    while node["referred"]:
        node = node["referred"][0]
        depth += 1
    assert depth == MAX_TREE_DEPTH
    assert node["truncated"] is True            # says it stopped early


def test_wide_node_is_capped_and_flagged(session):
    m = _merchant(session)
    c = _campaign(session, m)
    star = _user(session, 1)
    _buy(session, star, c, TODAY - timedelta(days=200))
    for n in range(2, MAX_TREE_CHILDREN + 6):
        _buy(session, _user(session, n), c, TODAY - timedelta(days=150),
             referred_by=star)

    tree = referral_tree(build_customers(m.id, session), star.id)

    assert len(tree["referred"]) == MAX_TREE_CHILDREN
    assert tree["truncated"] is True


def test_tree_is_scoped_to_this_merchant(session):
    """A referral that happened at another brand must not appear here."""
    mine, theirs = _merchant(session), _merchant(session, "other@example.com")
    my_deal, their_deal = _campaign(session, mine), _campaign(session, theirs)
    a, b = _user(session, 1), _user(session, 2)
    _buy(session, a, my_deal, TODAY - timedelta(days=100), tag="mine")
    # A referred B, but at the OTHER merchant.
    _buy(session, b, their_deal, TODAY - timedelta(days=90), referred_by=a,
         tag="theirs")

    tree = referral_tree(build_customers(mine.id, session), a.id)

    assert tree["referred"] == []
    assert tree["peopleBelow"] == 0


# ── Network depth ────────────────────────────────────────────────────────────

def test_depth_distinguishes_word_of_mouth_from_one_loud_poster(session):
    m = _merchant(session)
    c = _campaign(session, m)
    star = _user(session, 1)
    _buy(session, star, c, TODAY - timedelta(days=200))
    for n in (2, 3, 4):                          # all referred by the same person
        _buy(session, _user(session, n), c, TODAY - timedelta(days=150),
             referred_by=star)

    assert network_depth(build_customers(m.id, session)) == 1


def test_depth_counts_a_real_chain(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _chain(session, c, 4)

    assert network_depth(build_customers(m.id, session)) == 3


def test_depth_is_zero_without_referrals(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=30))

    assert network_depth(build_customers(m.id, session)) == 0


# ── Referrer quality ─────────────────────────────────────────────────────────

def test_quality_score_beats_raw_volume(session):
    """The whole point of the metric. One member brings four people who never
    return; another brings two who both become regulars. Volume says the first
    is better; quality says the second."""
    m = _merchant(session)
    c = _campaign(session, m)
    loud, good = _user(session, 1), _user(session, 2)
    _buy(session, loud, c, TODAY - timedelta(days=300))
    _buy(session, good, c, TODAY - timedelta(days=300))

    for n in range(10, 14):                      # four one-timers
        _buy(session, _user(session, n), c, TODAY - timedelta(days=200),
             referred_by=loud)
    for n in range(20, 22):                      # two who came back
        u = _user(session, n)
        _buy(session, u, c, TODAY - timedelta(days=200), referred_by=good)
        _buy(session, u, c, TODAY - timedelta(days=150), tag="repeat")

    scores = {s["userId"]: s for s in
              referrer_scores(build_customers(m.id, session), session, m.id)}

    assert scores[loud.id]["referred"] == 4
    assert scores[loud.id]["qualityScore"] == 0.0
    assert scores[good.id]["referred"] == 2
    assert scores[good.id]["qualityScore"] == 100.0


def test_return_on_bonus_uses_only_the_referrer_side(session):
    """A referral pays the referrer £1 and the referee 50p. Only the £1 is that
    referrer's cost — charging them the referee's half would understate them."""
    m = _merchant(session)
    c = _campaign(session, m)
    alice, bob = _user(session, 1), _user(session, 2)
    _buy(session, alice, c, TODAY - timedelta(days=200))
    receipt = _buy(session, bob, c, TODAY - timedelta(days=150),
                   referred_by=alice, total=50.0)
    session.add(ReferralReward(user_id=alice.id, kind="referrer",
                               receipt_id=receipt.id, campaign_id=c.id,
                               merchant_id=m.id, amount=1.0, status="available"))
    session.add(ReferralReward(user_id=bob.id, kind="referee",
                               receipt_id=receipt.id, campaign_id=c.id,
                               merchant_id=m.id, amount=0.5, status="available"))
    session.commit()

    (score,) = [s for s in referrer_scores(build_customers(m.id, session),
                                           session, m.id)
                if s["userId"] == alice.id]

    assert score["bonusesPaid"] == 1.0
    assert score["returnOnBonus"] == 50.0        # £50 of spend per £1 of bonus


def test_referrer_who_never_bought_here_is_skipped(session):
    """Shouldn't happen — receipts.py requires a referrer to have claimed the
    same deal — but the tree must not invent a node if the data ever says so."""
    m = _merchant(session)
    c = _campaign(session, m)
    ghost, buyer = _user(session, 1), _user(session, 2)
    _buy(session, buyer, c, TODAY - timedelta(days=100), referred_by=ghost)

    scores = referrer_scores(build_customers(m.id, session), session, m.id)
    assert scores == []


# ── The summary and the endpoints ────────────────────────────────────────────

def test_network_summary_multiplier(session):
    m = _merchant(session)
    c = _campaign(session, m)
    star = _user(session, 1)
    _buy(session, star, c, TODAY - timedelta(days=200))
    for n in (2, 3, 4):
        _buy(session, _user(session, n), c, TODAY - timedelta(days=150),
             referred_by=star)

    s = network_summary(build_customers(m.id, session), session, m.id)

    assert s["referrers"] == 1
    assert s["referredCustomers"] == 3
    assert s["multiplier"] == 3.0
    assert s["depth"] == 1


def test_network_summary_is_empty_without_referrals(session):
    m = _merchant(session)
    c = _campaign(session, m)
    _buy(session, _user(session, 1), c, TODAY - timedelta(days=30))

    s = network_summary(build_customers(m.id, session), session, m.id)

    assert s["multiplier"] == 0.0
    assert s["returnOnBonus"] == 0.0
    assert s["topReferrers"] == []


def test_tree_endpoint_404s_for_someone_elses_customer(session, client):
    from app.security import create_merchant_token

    mine, theirs = _merchant(session), _merchant(session, "other@example.com")
    _campaign(session, mine)
    stranger = _user(session, 1)
    _buy(session, stranger, _campaign(session, theirs),
         TODAY - timedelta(days=30))

    res = client.get(f"/merchant/customers/{stranger.id}/tree", headers={
        "Authorization": f"Bearer {create_merchant_token(mine)}"})

    assert res.status_code == 404


def test_tree_endpoint_returns_nested_json(session, client):
    from app.security import create_merchant_token

    m = _merchant(session)
    c = _campaign(session, m)
    a, b, d = _chain(session, c, 3)

    res = client.get(f"/merchant/customers/{a.id}/tree", headers={
        "Authorization": f"Bearer {create_merchant_token(m)}"})

    assert res.status_code == 200
    body = res.json()
    assert body["handle"] == "member1"
    assert body["peopleBelow"] == 2
    assert body["referred"][0]["referred"][0]["handle"] == "member3"
    assert "m1@x.com" not in res.text            # handle only, never the email
