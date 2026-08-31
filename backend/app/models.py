"""Database tables and the request/response shapes for the API.

`User` (table=True) is a real database table. The other classes are just the
JSON shapes we accept and return — kept separate so we never leak the password
hash to the browser.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    first_name: str
    last_name: str
    email: str = Field(index=True, unique=True)
    password_hash: str
    # Case-insensitively unique among non-blank handles — enforced by a partial
    # index in the referral-attribution migration, not expressible as a plain
    # SQLModel Field(unique=True) since blank ("" = no handle set) must repeat.
    instagram_handle: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Mention(SQLModel, table=True):
    """One Instagram post that tags the brand, owned by one user.

    PK is the Instagram post id (a post has a single owner, so it maps to one
    user). `user_id` links it back to its owner — the standard "one table per
    kind of user data, keyed by user_id" pattern.
    """
    id: str = Field(primary_key=True)                    # Instagram post id
    user_id: int = Field(index=True, foreign_key="user.id")
    url: Optional[str] = None
    display_url: Optional[str] = None
    caption: Optional[str] = None
    timestamp: Optional[str] = None
    owner_username: Optional[str] = None
    owner_full_name: Optional[str] = None
    likes_count: Optional[int] = None
    comments_count: Optional[int] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class Campaign(SQLModel, table=True):
    """A cashback deal shown to everyone — the global catalog (Phase 3).

    Unlike `Mention` this is NOT keyed by user; it's shared content authored
    through the admin form. `tags` and `images` hold JSON-encoded lists as TEXT
    so the shape is identical on SQLite and Postgres (no DB-specific JSON type).
    Columns are the union of what `deal.html` and `browse.html` render.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    brand: str = ""
    title: str = ""            # long detail title (deal.html hero)
    card_title: str = ""       # short listing title (browse card)
    card_desc: str = ""        # short listing description (browse card)
    long_desc: str = ""        # deal.html desc2
    emoji: str = ""            # fallback thumbnail when no images yet
    category: str = ""
    rate: float = 0            # cashback %
    earn: str = ""             # e.g. "£13.00"
    spend_desc: str = ""       # deal.html desc, e.g. "on a £100 spend"
    total_paid: str = ""       # e.g. "£112,705 paid to members"
    members: str = ""          # e.g. "1.8k"
    claims: int = 0            # browse "claims" count
    expiry: str = ""           # "30 Jun 2026" / "Ongoing" / "New members only"
    location: str = ""         # e.g. "Online · UK"
    terms: str = ""            # HTML string
    brand_url: str = ""        # outbound shop link
    bg: str = "var(--paper-deep)"
    tags: str = "[]"           # JSON-encoded list[str]
    images: str = "[]"         # JSON-encoded list[str] of image URLs
    merchant_id: Optional[int] = Field(default=None, foreign_key="merchant.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MerchantApplication(SQLModel, table=True):
    """A brand's partnership application, submitted from contact.html.

    Public (no user account needed) — merchants aren't Cirqle users. The admin
    reviews these on admin.html and, on approve, a live `Campaign` is created
    from the key fields (`campaign_id` links to it). Lifecycle:
      pending -> approved (deal published)  or  rejected.
    `goals` holds a JSON-encoded list[str] as TEXT (same trick as Campaign.tags).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    brand: str = ""
    website: str = ""
    category: str = ""
    cashback_rate: float = 0          # target cashback %, becomes the deal rate
    markets: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    role: str = ""
    revenue: str = ""
    orders: str = ""
    aov: str = ""                     # avg order value (£), kept as text (may be blank)
    budget: str = ""
    timeline: str = ""
    goals: str = "[]"                 # JSON-encoded list[str]
    heard: str = ""
    message: str = ""
    status: str = "pending"           # pending -> approved / rejected
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None


class Receipt(SQLModel, table=True):
    """A cashback claim: a receipt a user uploaded for a deal (Phase 4 + 5).

    Per-user data keyed by `user_id` (like Mention). Doubles as the cashback
    ledger — every £ figure on the dashboard is derived from these rows, so no
    separate transactions table is needed. Lifecycle:
      pending (uploaded) -> confirmed (admin-verified) -> paid (withdrawn);  or rejected.
    `brand`/`amount` are snapshotted from the deal at upload so the claim stays
    correct even if the campaign is later edited. `image_key` is a private storage
    key (never a public URL; owner/admin view via short-lived presigned links).
    A claim whose tagged post mentions the brand is auto-confirmed at upload.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    post_id: str = Field(index=True)                 # the Instagram post it proves
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    brand: str = ""                                  # snapshot of the deal's brand
    amount: float = 0                                # cashback £ (snapshot of deal.earn)
    image_key: str                                   # private storage key
    image_sha256: str = ""                           # content hash, for duplicate detection
    status: str = "pending"                          # pending -> confirmed -> paid / rejected
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    referred_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")


class Merchant(SQLModel, table=True):
    """A brand's login to the merchant portal (Phase 6).

    Merchants aren't `User`s — an admin creates this from an *approved*
    `MerchantApplication` and gives the brand the generated password. Their
    deals are the `Campaign` rows whose `merchant_id` points here, so the
    portal can show real per-merchant stats.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: Optional[int] = Field(default=None, foreign_key="merchantapplication.id")
    email: str = Field(index=True, unique=True)
    password_hash: str
    business_name: str = ""
    # ── Public brand profile (Phase 6b) — how members see the brand in the app ──
    bio: str = ""
    categories: str = "[]"       # JSON-encoded list[str] (same trick as Campaign.tags)
    website: str = ""
    instagram: str = ""          # handle, stored without the leading @
    tiktok: str = ""
    youtube: str = ""
    facebook: str = ""
    tips: str = ""               # tips the brand gives shoppers (free text)
    logo_url: str = ""           # public S3 URL of the uploaded logo
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MerchantTransaction(SQLModel, table=True):
    """A prepaid credit top-up on a merchant's account (Phase 6b billing).

    Merchants pre-fund a balance that pays for shopper cashback. This table is
    the top-ups only; cashback spend is derived from the merchant's `Receipt`
    rows, and the live balance = sum(top-ups) - cashback given. (No real payment
    processor yet — a top-up is recorded, not charged.)
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: int = Field(index=True, foreign_key="merchant.id")
    kind: str = "topup"          # "topup"
    amount: float = 0            # £ added
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DealEvent(SQLModel, table=True):
    """One tracked interaction with a deal — a page view or an outbound click.

    Append-only log (Phase 6). Fired from deal.html via a beacon; aggregated
    into the merchant dashboard. Public/anonymous — no user_id.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(index=True, foreign_key="campaign.id")
    kind: str = Field(index=True)                     # "view" | "click"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MerchantMessage(SQLModel, table=True):
    """A message in a merchant<->admin thread (Phase 6).

    Merchants message the admin (general note or a `deal_request` to get more
    deals); the admin replies. One flat thread per merchant.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: int = Field(index=True, foreign_key="merchant.id")
    sender: str = "merchant"                          # "merchant" | "admin"
    kind: str = "message"                             # "message" | "deal_request"
    body: str = ""
    read_by_admin: bool = False
    read_by_merchant: bool = True                     # merchant has seen their own message
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignSubmission(SQLModel, table=True):
    """A deal a merchant proposes from the portal, awaiting admin review (Phase 6).

    On approve, a live `Campaign` is created from these fields (attributed to the
    merchant via `merchant_id`) and `campaign_id` links to it. On reject, the
    admin's `rejection_reason` is shown back to the merchant in their portal.
    Lifecycle: pending -> approved / rejected.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: int = Field(index=True, foreign_key="merchant.id")
    brand: str = ""              # snapshot of the merchant's business name
    card_title: str = ""
    card_desc: str = ""
    long_desc: str = ""
    category: str = ""
    rate: float = 0              # cashback %
    earn: str = ""               # e.g. "£10.00"
    spend_desc: str = ""         # e.g. "on a £50 spend"
    expiry: str = ""
    location: str = ""
    brand_url: str = ""
    terms: str = ""
    status: str = "pending"      # pending -> approved / rejected
    rejection_reason: str = ""
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None


class AdminActivity(SQLModel, table=True):
    """A log of admin actions (approve/reject applications + campaign submissions,
    verify/reject receipt claims, create merchant logins, reply to merchants).
    Shown as a scrollable feed at the bottom of admin.html."""
    id: Optional[int] = Field(default=None, primary_key=True)
    action: str = ""      # short label, e.g. "Approved campaign submission"
    detail: str = ""      # context, e.g. "Nike — 20% off summer (deal #14)"
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── What the browser sends ──
class SignupIn(BaseModel):
    firstName: str
    lastName: str
    email: EmailStr
    password: str
    instagramHandle: str


class SigninIn(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdateIn(BaseModel):
    # Only fields the user is allowed to change. Optional -> PATCH semantics
    # (send just what you want to update; omitted fields are left untouched).
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    email: Optional[EmailStr] = None
    instagramHandle: Optional[str] = None


class PasswordChangeIn(BaseModel):
    currentPassword: str
    newPassword: str


# ── What the API returns (never includes the password hash) ──
class UserOut(BaseModel):
    firstName: str
    lastName: str
    email: EmailStr
    instagramHandle: str
    createdAt: Optional[datetime] = None


class AuthOut(BaseModel):
    token: str
    user: UserOut


# ── Instagram feed (Phase 2) ──
# A trimmed post shape: only the fields the feed page renders, so we never leak
# the rest of the raw scrape to the browser.
class FeedPost(BaseModel):
    id: Optional[str] = None
    url: Optional[str] = None
    displayUrl: Optional[str] = None
    caption: Optional[str] = None
    timestamp: Optional[str] = None
    ownerUsername: Optional[str] = None
    ownerFullName: Optional[str] = None
    likesCount: Optional[int] = None
    commentsCount: Optional[int] = None


class FeedRefreshOut(BaseModel):
    posts: list[FeedPost]
    updated: Optional[datetime] = None   # None when the user has no stored posts yet


# ── Campaigns (Phase 3) ──
class CampaignIn(BaseModel):
    """The text fields the admin form sends (as a JSON payload alongside the
    uploaded image files). Every field is optional so PATCH can send a partial
    update — the router only applies the fields that were actually provided.
    """
    brand: Optional[str] = None
    title: Optional[str] = None
    cardTitle: Optional[str] = None
    cardDesc: Optional[str] = None
    longDesc: Optional[str] = None
    emoji: Optional[str] = None
    category: Optional[str] = None
    rate: Optional[float] = None
    earn: Optional[str] = None
    spendDesc: Optional[str] = None
    totalPaid: Optional[str] = None
    members: Optional[str] = None
    claims: Optional[int] = None
    expiry: Optional[str] = None
    location: Optional[str] = None
    terms: Optional[str] = None
    brandUrl: Optional[str] = None
    bg: Optional[str] = None
    tags: Optional[list[str]] = None


class CampaignOut(BaseModel):
    id: int
    brand: str
    title: str
    cardTitle: str
    cardDesc: str
    longDesc: str
    emoji: str
    category: str
    rate: float
    earn: str
    spendDesc: str
    totalPaid: str
    members: str
    claims: int
    expiry: str
    location: str
    terms: str
    brandUrl: str
    bg: str
    tags: list[str]
    images: list[str]


# ── Receipts / cashback (Phase 4 + 5) ──
# Receipts stay private on S3; the owner (and admin) view them via short-lived
# presigned URLs only.
class ReceiptOut(BaseModel):
    id: int
    postId: str
    campaignId: Optional[int] = None
    brand: str
    amount: float
    status: str
    uploadedAt: datetime
    imageUrl: Optional[str] = None   # presigned GET (owner viewing); None in local mode


class AdminReceiptOut(BaseModel):
    """Admin review view — includes who submitted it + a short-lived image URL."""
    id: int
    userEmail: str
    userName: str
    postId: str
    brand: str
    amount: float
    status: str
    uploadedAt: datetime
    imageUrl: Optional[str] = None   # presigned GET, expires shortly


class ActivityItem(BaseModel):
    brand: str
    amount: float
    status: str
    date: datetime
    imageUrl: Optional[str] = None   # presigned link to the user's own receipt


class AccountStats(BaseModel):
    """Real per-user dashboard numbers, all derived from the user's receipts."""
    totalEarned: float   # confirmed + paid
    pending: float       # awaiting verification
    wallet: float        # confirmed, available to withdraw
    paidOut: float       # already withdrawn
    brandsUsed: int
    postsCount: int
    receiptsCount: int
    activity: list[ActivityItem]


# ── Merchant partnership applications ──
class MerchantApplicationIn(BaseModel):
    """What contact.html's partnership form sends. The key fields needed to
    publish a deal on approval are required; the rest is optional context."""
    brand: str
    website: str
    category: str
    cashbackRate: float
    firstName: str
    lastName: str
    email: EmailStr
    markets: str = ""
    phone: str = ""
    role: str = ""
    revenue: str = ""
    orders: str = ""
    aov: str = ""
    budget: str = ""
    timeline: str = ""
    goals: list[str] = []
    heard: str = ""
    message: str = ""


class MerchantApplicationOut(BaseModel):
    """The admin review view — the full application plus its status."""
    id: int
    brand: str
    website: str
    category: str
    cashbackRate: float
    markets: str
    firstName: str
    lastName: str
    email: str
    phone: str
    role: str
    revenue: str
    orders: str
    aov: str
    budget: str
    timeline: str
    goals: list[str]
    heard: str
    message: str
    status: str
    campaignId: Optional[int] = None
    createdAt: datetime


# ── Merchant portal (Phase 6) ──
class MerchantSigninIn(BaseModel):
    email: EmailStr
    password: str


class MerchantOut(BaseModel):
    id: int
    email: EmailStr
    businessName: str
    applicationId: Optional[int] = None
    createdAt: datetime


class MerchantAuthOut(BaseModel):
    token: str
    merchant: MerchantOut


class MerchantCreateIn(BaseModel):
    """Admin: turn an approved application into a merchant login."""
    applicationId: int


class MerchantCreatedOut(BaseModel):
    """Returned once to the admin so they can pass on the credentials.
    The plaintext password is shown here and never stored or shown again."""
    merchant: MerchantOut
    password: str


class DealStat(BaseModel):
    campaignId: int
    brand: str
    title: str
    views: int
    clicks: int
    claims: int
    cashback: float


class TimePoint(BaseModel):
    date: str          # YYYY-MM-DD
    views: int
    clicks: int
    claims: int


class MerchantStats(BaseModel):
    """Real per-merchant dashboard numbers, aggregated across the merchant's deals."""
    dealsCount: int
    views: int
    clicks: int
    claims: int
    cashbackGiven: float     # confirmed + paid
    pendingCashback: float   # awaiting verification
    conversion: float        # claims / views, as a percentage
    timeseries: list[TimePoint]
    deals: list[DealStat]


class MerchantMessageIn(BaseModel):
    body: str
    kind: str = "message"    # "message" | "deal_request"


class MerchantMessageOut(BaseModel):
    id: int
    sender: str
    kind: str
    body: str
    createdAt: datetime


class MerchantThreadOut(BaseModel):
    """Admin view of one merchant's message thread."""
    merchantId: int
    businessName: str
    email: str
    unread: int              # messages from the merchant the admin hasn't read
    messages: list[MerchantMessageOut]


class AdminMessageIn(BaseModel):
    merchantId: int
    body: str


# ── Deal-event tracking (Phase 6) ──
class EventIn(BaseModel):
    campaignId: int
    kind: str                # "view" | "click"


# ── Merchant-submitted campaigns (Phase 6) ──
class CampaignSubmissionIn(BaseModel):
    """What the merchant portal sends to propose a new deal. Brand is taken from
    the merchant's account, not the form."""
    cardTitle: str
    cardDesc: str = ""
    longDesc: str = ""
    category: str = ""
    rate: float = 0
    earn: str = ""
    spendDesc: str = ""
    expiry: str = ""
    brandUrl: str = ""
    terms: str = ""


class CampaignSubmissionOut(BaseModel):
    id: int
    brand: str
    cardTitle: str
    cardDesc: str
    longDesc: str
    category: str
    rate: float
    earn: str
    spendDesc: str
    expiry: str
    location: str
    brandUrl: str
    terms: str
    status: str
    rejectionReason: str
    campaignId: Optional[int] = None
    createdAt: datetime


class RejectSubmissionIn(BaseModel):
    reason: str


# ── Merchant brand profile (Phase 6b) ──
class MerchantProfileIn(BaseModel):
    """Fields the merchant can edit on their brand profile. All optional so a
    PATCH only touches what's sent (the logo is uploaded separately)."""
    businessName: Optional[str] = None
    bio: Optional[str] = None
    categories: Optional[list[str]] = None
    website: Optional[str] = None
    instagram: Optional[str] = None
    tiktok: Optional[str] = None
    youtube: Optional[str] = None
    facebook: Optional[str] = None
    tips: Optional[str] = None


class MerchantProfileOut(BaseModel):
    id: int
    email: EmailStr
    businessName: str
    bio: str
    categories: list[str]
    website: str
    instagram: str
    tiktok: str
    youtube: str
    facebook: str
    tips: str
    logoUrl: str
    createdAt: datetime


# ── Posts tagging the merchant (Phase 6b) ──
class TaggedPostOut(BaseModel):
    """One Instagram post that tagged this merchant (via a shopper's claim),
    with its engagement counts. `views` is only known for videos/reels — it's
    None for photo posts, which Instagram doesn't expose a view count for."""
    postId: str
    imageUrl: Optional[str] = None
    caption: str = ""
    ownerUsername: str = ""
    likes: Optional[int] = None
    comments: Optional[int] = None
    views: Optional[int] = None
    url: Optional[str] = None
    dealTitle: str = ""
    date: Optional[str] = None


# ── Referral attribution (Phase 8) ──
class ReferralStat(BaseModel):
    """One referrer's track record on one campaign: claims they referred and
    the originating post, for the merchant's "top referring posts" panel."""
    referrerUserId: int
    referrerHandle: str
    campaignId: int
    brand: str
    dealTitle: str
    postId: Optional[str] = None      # referrer's own claimed post for this campaign, if found
    imageUrl: Optional[str] = None
    claims: int                        # number of claims they referred
    referredCashback: float            # sum of the referred claims' amounts (not a reward figure)


# ── Merchant billing / prepaid balance (Phase 6b) ──
class BillingTxnOut(BaseModel):
    kind: str            # "topup" | "cashback"
    amount: float        # positive for a top-up, negative for cashback given
    description: str
    date: datetime


class BillingOut(BaseModel):
    balance: float           # sum(top-ups) - cashback given
    totalToppedUp: float
    cashbackGiven: float     # confirmed + paid
    pendingCashback: float   # awaiting verification (not yet deducted)
    transactions: list[BillingTxnOut]


class TopUpIn(BaseModel):
    amount: float


# ── Admin activity log (Phase 6) ──
class AdminActivityOut(BaseModel):
    id: int
    action: str
    detail: str
    createdAt: datetime


# ── Admin analytics (Phase 7) ──
# All derived from the tables above on read — nothing extra is stored.
class AdminTimePoint(BaseModel):
    date: str          # YYYY-MM-DD
    views: int
    clicks: int
    claims: int
    signups: int       # new member accounts that day


class AdminCompanyStat(BaseModel):
    """One onboarded company (a merchant login) and everything it has done."""
    merchantId: int
    name: str
    email: str
    joinedAt: datetime
    logoUrl: str = ""
    deals: int                 # live deals attributed to this company
    pendingSubmissions: int    # deals it has proposed, awaiting review
    views: int
    clicks: int
    claims: int
    ctr: float                 # clicks / views, as a percentage
    conversion: float          # claims / views, as a percentage
    cashbackGiven: float       # confirmed + paid
    pendingCashback: float     # claims still clearing / awaiting approval
    toppedUp: float
    balance: float             # top-ups - cashback given
    unreadMessages: int
    shortfall: float           # pending cashback the prepaid balance can't cover (0 = funded)
    atRisk: bool               # True when shortfall > 0
    lastActiveAt: Optional[datetime] = None
    daysSinceActive: Optional[int] = None
    status: str                # "active" (7d) | "quiet" (30d) | "dormant"
    spark: list[int]           # daily activity (views+clicks+claims), last 30 days


class AdminCategoryStat(BaseModel):
    category: str
    deals: int
    views: int
    claims: int
    cashback: float


class AdminDealStat(BaseModel):
    campaignId: int
    brand: str
    title: str
    company: str               # owning merchant, or "" for in-house deals
    views: int
    clicks: int
    claims: int
    cashback: float


class AdminMemberStat(BaseModel):
    """One member and their cashback ledger — the admin's view of a shopper."""
    userId: int
    name: str
    email: str
    instagramHandle: str = ""
    joinedAt: datetime
    posts: int
    claims: int
    earned: float              # cleared cashback ever (confirmed + paid)
    wallet: float              # cleared and still withdrawable
    paidOut: float             # already withdrawn
    pending: float             # awaiting approval / still clearing
    expired: float             # lapsed unapproved
    rejected: int              # rejected claims
    brandsUsed: int
    lastClaimAt: Optional[datetime] = None


class AdminFraudSignal(BaseModel):
    """One thing worth a second look before cashback goes out.

    These are heuristics, not verdicts — every one has an innocent explanation
    and is meant to be reviewed, not auto-rejected.
    """
    kind: str                  # repeat_claims | fresh_account | duplicate_image | shared_post
    severity: str              # "high" | "watch"
    title: str
    detail: str
    member: str = ""           # "Name — email" of the member involved
    amount: float = 0          # £ across the flagged claims (some may already be paid)
    count: int = 0
    receiptIds: list[int] = []


class AdminBulkVerifyIn(BaseModel):
    ids: list[int]


class AdminBulkVerifyOut(BaseModel):
    """Result of a bulk approve — per-claim outcomes, since some may be too old."""
    approved: int
    failed: int
    errors: list[str] = []


class AdminQueue(BaseModel):
    """What is waiting on the admin right now."""
    pendingReceipts: int
    pendingApplications: int
    pendingSubmissions: int
    unreadMessages: int
    expiringSoon: int          # claims that lapse within 24h unless approved


class AdminAnalyticsOut(BaseModel):
    # Companies
    companiesOnboard: int
    companiesActiveInWindow: int
    companiesNewInWindow: int
    companiesDormant: int
    # Pipeline (applications)
    applicationsTotal: int
    applicationsPending: int
    applicationsApproved: int
    applicationsRejected: int
    approvalRate: float        # approved / (approved + rejected), %
    avgReviewHours: Optional[float] = None
    # Members + catalog
    members: int
    membersNewInWindow: int
    deals: int
    dealsFromCompanies: int    # deals attributed to a merchant login
    # Engagement
    views: int
    clicks: int
    claims: int
    ctr: float
    conversion: float
    # Money
    cashbackGiven: float
    pendingCashback: float
    expiredCashback: float
    rejectedClaims: int
    toppedUp: float
    outstandingBalance: float  # total prepaid credit not yet spent
    avgClaimValue: float
    companiesAtRisk: int       # companies whose balance can't cover pending cashback
    totalShortfall: float
    # Members
    walletOwed: float          # cleared cashback members can still withdraw
    paidOut: float             # already withdrawn
    # Breakdowns
    windowDays: int            # the trend/activity window these numbers use
    queue: AdminQueue
    timeseries: list[AdminTimePoint]
    companies: list[AdminCompanyStat]
    topMembers: list[AdminMemberStat]
    fraud: list[AdminFraudSignal]
    categories: list[AdminCategoryStat]
    topDeals: list[AdminDealStat]
    generatedAt: datetime
