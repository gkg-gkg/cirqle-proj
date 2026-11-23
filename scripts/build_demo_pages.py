"""Build the private merchant demo pages under for/ from the Boswell St Barbers masters.

The two hand-made pages are the templates:
    for/boswell-st-barbers.html        (original design)
    for/boswell-st-barbers-story.html  (scroll-story design)

For every brand in scripts/demo_brands.json this swaps in the brand's name,
address, category wording, photos, prices and example figures, and writes
    for/<slug>.html  and  for/<slug>-story.html

Usage:  python3 scripts/build_demo_pages.py
"""
import html
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTERS = {'': ROOT / 'for/boswell-st-barbers.html',
           '-story': ROOT / 'for/boswell-st-barbers-story.html'}

# ── Category wording, photos and typical spend ──────────────────────────────
# match: regex on the sheet's category. First match wins.
# spend: typical spend by the sheet's price band (£, ££, £££ …).
KINDS = [
    dict(key='barber', match=r'barber', short='Barber', chip='Haircuts', item='Haircut',
         visit='gets her cut', buys='gets her cut', sam='He gets a cut', want='Her friend wants the same cut',
         one='One haircut, followed through', two='Two customers for one haircut',
         caption='fresh cut 💈', ask='who did your hair?? 🔥', pick='barber', ico='💈',
         regular='Your customers come back every 3–5 weeks. Every cut is another chance to share you.',
         p1='/assets/demo/barber-pole.jpg', p2='/assets/demo/pole-mural.jpg', spend={'£': 15, '££': 25}),
    dict(key='salon', match=r'hair|salon|beauty', short='Hair salon', chip='Hair', item='Hair appointment',
         visit='gets her hair done', buys='gets her hair done', sam='He books in', want='Her friend wants the same look',
         one='One appointment, followed through', two='Two customers for one appointment',
         caption='new hair, who dis 💇', ask='your hair looks amazing, where did you go??', pick='salon', ico='💇',
         regular='Your customers come back every few weeks. Every appointment is another chance to share you.',
         p1='/assets/brands/brushes.jpg', p2='/assets/wall-hd/h12.jpg', spend={'£': 25, '££': 45}),
    dict(key='bubble', match=r'bubble tea', short='Bubble tea', chip='Bubble tea', item='Bubble tea',
         visit='grabs a bubble tea', buys='orders her bubble tea', sam='He orders', want='Her friend wants one too',
         one='One drink, followed through', two='Two customers for one drink',
         caption='new fave bubble tea 🧋', ask="ok where's that from??", pick='bubble tea spot', ico='🧋',
         regular='Bubble tea is a regular treat. Every visit is another chance to share you.',
         p1='/assets/demo/bubble-tea.jpg', p2='/assets/demo/bubble-tea-2.jpg', spend={'£': 5.5, '££': 6.5}),
    dict(key='gelato', match=r'gelato|ice cream', short='Gelato', chip='Gelato', item='Gelato',
         visit='gets a gelato', buys='gets her gelato', sam='He gets a gelato', want='Her friend wants one too',
         one='One gelato, followed through', two='Two customers for one gelato',
         caption='best gelato around 🍨', ask='omg where is that from', pick='dessert spot', ico='🍨',
         regular='A cheap treat people come back for. Every visit is another chance to share you.',
         p1='/assets/demo/ice-cream.jpg', p2='/assets/demo/ice-cream.jpg', spend={'£': 4.5, '££': 6}),
    dict(key='bakery', match=r'bakery|patisserie|cake|bakehouse', short='Bakery', chip='Bakery', item='Pastries',
         visit='picks up a pastry', buys='picks up her pastries', sam='He picks some up', want='Her friend wants one too',
         one='One pastry, followed through', two='Two customers for one pastry',
         caption='these are unreal 🥐', ask='where are those from??', pick='bakery', ico='🥐',
         regular='Bakery runs become a habit. Every visit is another chance to share you.',
         p1='/assets/demo/pastry.jpg', p2='/assets/demo/pastry-2.jpg', spend={'£': 4, '££': 6, '£–££': 5}),
    dict(key='food', match=r'street food|takeaway|mediterranean|caribbean|market|mexican|indian|lebanese|vietnamese', short='Street food', chip='Lunch', item='Lunch',
         visit='grabs lunch', buys='grabs her lunch', sam='He grabs lunch', want='Her friend wants the same lunch',
         one='One lunch, followed through', two='Two customers for one lunch',
         caption='best lunch around here 🌯', ask='omg what is that, looks so good', pick='lunch spot', ico='🍽️',
         regular='Lunch is a daily habit. Every visit is another chance to share you.',
         p1='/assets/cats/food.jpg', p2='/assets/wall-hd/h10.jpg', spend={'£': 8, '£–££': 10, '££': 12}),
    dict(key='restaurant', match=r'restaurant', short='Restaurant', chip='Dining', item='Dinner',
         visit='has dinner', buys='has her meal', sam='He books a table', want='Her friend wants to go too',
         one='One meal, followed through', two='Two customers for one meal',
         caption='dinner was unreal 🍽️', ask='where did you eat?? looks so good', pick='restaurant', ico='🍽️',
         regular='Good food gets talked about. Every meal is another chance to share you.',
         p1='/assets/cats/food.jpg', p2='/assets/wall-hd/h10.jpg', spend={'££': 20, '£££': 35}),
    dict(key='cafe', match=r'coffee|café|cafe|deli|brunch|breakfast|restaurant', short='Café', chip='Coffee', item='Coffee & cake',
         visit='buys her coffee', buys='orders her coffee', sam='He orders', want='Her friend wants the same',
         one='One coffee, followed through', two='Two customers for one coffee',
         caption='new favourite spot ☕️', ask="where's that from??", pick='café', ico='☕️',
         regular='Coffee is a daily habit. Every visit is another chance to share you.',
         p1='/assets/brands/latte.jpg', p2='/assets/wall-hd/h2.jpg', spend={'£': 4.5, '££': 7, '£££': 30}),
    dict(key='records', match=r'record', short='Record shop', chip='Vinyl', item='Records',
         visit='picks up a record', buys='picks up her records', sam='He picks up a record', want='Her friend wants a dig too',
         one='One record, followed through', two='Two customers for one record',
         caption='vinyl haul 🎶', ask='where did you find that??', pick='record shop', ico='🎶',
         regular='Collectors come back for the next find. Every visit is another chance to share you.',
         p1='/assets/brands/headphones.jpg', p2='/assets/wall-hd/h13.jpg', spend={'£': 15, '££': 25}),
    dict(key='books', match=r'book', short='Bookshop', chip='Books', item='Books',
         visit='picks up a book', buys='picks up her books', sam='He picks up a book', want='Her friend wants a look too',
         one='One book, followed through', two='Two customers for one book',
         caption='book haul 📚', ask='where did you get that??', pick='bookshop', ico='📚',
         regular='Readers come back for the next one. Every visit is another chance to share you.',
         p1='/assets/demo/bookshop.jpg', p2='/assets/wall-hd/h9.jpg', spend={'£': 10, '££': 15}),
    dict(key='florist', match=r'florist', short='Florist', chip='Flowers', item='Flowers',
         visit='buys some flowers', buys='buys her flowers', sam='He buys a bunch', want='Her friend wants some too',
         one='One bouquet, followed through', two='Two customers for one bouquet',
         caption='how pretty are these 💐', ask='those are gorgeous, where from?', pick='florist', ico='💐',
         regular='Birthdays, dates, thank-yous. Every bunch is another chance to share you.',
         p1='/assets/demo/flowers.jpg', p2='/assets/demo/flowers-2.jpg', spend={'£': 15, '££': 30}),
    dict(key='plants', match=r'plant', short='Plant shop', chip='Plants', item='Plants',
         visit='picks up a plant', buys='picks up her plant', sam='He picks one up', want='Her friend wants one too',
         one='One plant, followed through', two='Two customers for one plant',
         caption='new plant baby 🪴', ask='where did you get that plant?', pick='plant shop', ico='🪴',
         regular='One plant turns into a collection. Every visit is another chance to share you.',
         p1='/assets/demo/plants.jpg', p2='/assets/demo/plants-2.jpg', spend={'£': 15, '££': 25}),
    dict(key='bike', match=r'bike', short='Bike repair', chip='Repairs', item='Bike service',
         visit='gets her bike fixed', buys='gets her bike fixed', sam='He brings his bike in', want='Her friend needs a fix too',
         one='One repair, followed through', two='Two customers for one repair',
         caption='back on the road 🚲', ask='your bike looks brand new, who fixed it?', pick='bike shop', ico='🚲',
         regular='Bikes need looking after all year. Every service is another chance to share you.',
         p1='/assets/demo/bike.jpg', p2='/assets/demo/bike-2.jpg', spend={'£': 20, '££': 30}),
    dict(key='jewellery', match=r'jewel', short='Jewellery', chip='Jewellery', item='Jewellery',
         visit='buys a piece', buys='buys her piece', sam='He buys a gift', want='Her friend wants one too',
         one='One piece, followed through', two='Two customers for one piece',
         caption='obsessed with this ✨', ask='where is that from?!', pick='jeweller', ico='💍',
         regular='Gifts and treats all year round. Every piece is another chance to share you.',
         p1='/assets/brands/rail.jpg', p2='/assets/wall-hd/h1.jpg', spend={'£': 25, '££': 45}),
    dict(key='grocery', match=r'grocery|supermarket', short='Grocery', chip='Groceries', item='Groceries',
         visit='does her shop', buys='does her shop', sam='He does his shop', want='Her friend wants to try it',
         one='One shop, followed through', two='Two customers for one shop',
         caption='best little grocery 🥬', ask='where do you get your food?', pick='grocery', ico='🥬',
         regular='Everyone shops every week. Every visit is another chance to share you.',
         p1='/assets/cats/food.jpg', p2='/assets/wall-hd/h10.jpg', spend={'£': 12, '££': 20}),
    dict(key='tailor', match=r'tailor', short='Tailor', chip='Tailoring', item='Tailoring',
         visit='gets her suit fitted', buys='gets her suit fitted', sam='He books a fitting', want='Her friend needs one too',
         one='One fitting, followed through', two='Two customers for one fitting',
         caption='graduation ready 🎓', ask='that fits perfectly, where from?', pick='tailor', ico='🧵',
         regular='Graduations, interviews, weddings. Every fitting is another chance to share you.',
         p1='/assets/brands/jackets.jpg', p2='/assets/wall-hd/h14.jpg', spend={'££': 80, '£££': 150}),
    dict(key='fashion', match=r'fashion|vintage|charity|concept', short='Fashion', chip='Clothing', item='Clothing',
         visit='buys a new piece', buys='buys her new piece', sam='He buys something', want='Her friend wants a look too',
         one='One outfit, followed through', two='Two customers for one outfit',
         caption='new fit ✨', ask="wait where's that from?!", pick='shop', ico='🛍️',
         regular='Every new piece is another chance for customers to share you.',
         p1='/assets/brands/rail.jpg', p2='/assets/wall-hd/h1.jpg', spend={'£': 15, '££': 40, '££–£££': 60, '£££': 80}),
    dict(key='gifts', match=r'.', short='Gift shop', chip='Gifts', item='Gift',
         visit='picks up a gift', buys='picks up her gift', sam='He picks up a gift', want='Her friend wants one too',
         one='One gift, followed through', two='Two customers for one gift',
         caption='found the perfect gift 🎁', ask='where did you get that??', pick='gift shop', ico='🎁',
         regular='Birthdays and thank-yous all year. Every gift is another chance to share you.',
         p1='/assets/cats/home.jpg', p2='/assets/wall-hd/h14.jpg', spend={'£': 15, '££': 25, '£££': 40}),
]

AREAS = [(r'Leather Lane|Hatton Garden|EC1N', 'Holborn'), (r'WC1V|Procter', 'Holborn'),
         (r'N1C|Coal Drops|Granary|Canopy|Regent', "King's Cross"),
         (r'WC1X|WC1H|Cromer|Judd St|Leigh St', "King's Cross"),
         (r'W1\b|Fitzrovia|Tottenham Court', 'Fitzrovia'), (r'.', 'Bloomsbury')]

BIG_IG = 7000   # followers: from here up, the FAQ asks about a big following instead


def kind_of(cat):
    return next(k for k in KINDS if re.search(k['match'], cat, re.I))


def money(v):
    return f'£{v:,.2f}' if v % 1 else f'£{int(v):,}'


def money2(v):
    return f'£{v:,.2f}'


def slugify(name):
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    s = s.lower().replace('&', 'and').replace("'", '')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def initials(name):
    words = [w for w in re.findall(r"[A-Za-z0-9']+", name) if w.lower() not in ('the', 'and', 'by')]
    return ''.join(w[0].upper() for w in words[:2])


def render(master, b, k, story):
    e = lambda s: html.escape(s, quote=True)
    t = master
    name, up = e(b['name']), e(b['name'].upper())
    spend = b['spend']
    gave = round(spend * 0.10 + 1.5, 2)
    sales, cost = round(spend * 34, 2), round(spend * 34 * 0.10 + 11 * 1.5, 2)
    handle = b['handle']
    tag_sticker = e(handle) if handle else '📍 ' + name

    R = [  # (old, new, expected count: None = any, including 0)
        ('boswell-st-barbers-story', b['slug'] + '-story', None),
        ('boswell-st-barbers', b['slug'], None),
        ('For Boswell St Barbers · 20 Boswell St', f'For {name} · {e(b["addr_short"])}', 1),
        ('20 Boswell St, WC1N 3BW', e(b['addr_full']), None),
        ('BOSWELL ST BARBERS', up, None),
        ('<div class="sticker">@boswellstbarbers</div>', f'<div class="sticker">{tag_sticker}</div>', None),
        ('<span class="ptag">@boswellstbarbers</span>', f'<span class="ptag">{tag_sticker}</span>', None),
        (' thank you <a>@boswellstbarbers</a>', f' <a>{e(handle)}</a>' if handle else '', None),
        ('tagging @boswellstbarbers', f'tagging {e(handle) if handle else name}', None),
        ('<b>Boswell St</b>', f'<b>{e(b["street"])}</b>', None),
        ('Barber · Bloomsbury', f'{e(k["short"])} · {e(b["area"])}', None),
        ('<span>Haircuts</span>', f'<span>{e(k["chip"])}</span>', None),
        ('Boswell St Barbers', name, None),
        ('<span class="mono">BS</span>', f'<span class="mono">{b["ini"]}</span>', None),
        ('<div class="mono">BS</div>', f'<div class="mono">{b["ini"]}</div>', None),
        ('<span class="ci b">BS</span>', f'<span class="ci b">{b["ini"]}</span>', None),
        ('/assets/demo/barber-pole.jpg', k['p1'], None),
        ('/assets/demo/pole-mural.jpg', k['p2'], None),
        ('One haircut, followed through', k['one'], None),
        ('gets her cut at', f'{k["visit"]} at', None),
        ('She gets her cut and pays as normal.', f'She {k["buys"]} and pays as normal.', None),
        ('He gets a cut, uploads', f'{k["sam"]}, uploads', None),
        ('Her friend wants the same cut', k['want'], None),
        ('Two customers for one haircut', k['two'], None),
        ('<span>Student cut</span>', f'<span>{e(b["receipt_item"])}</span>', None),
        ('<span>Haircut</span>', f'<span>{e(k["item"])}</span>', None),
        ('fresh cut 💈', k['caption'], None),
        ('who did your hair?? 🔥', e(k['ask']), None),
        ('Most people pick a barber because a friend told them to.',
         f'Most people pick a {k["pick"]} because a friend told them to.', None),
        ('How people pick a barber', f'How people pick a {k["pick"]}', None),
        ('Your customers come back every 3–5 weeks. Every cut is another chance to share you.', k['regular'], None),
        ('<div class="ico">💈</div>', f'<div class="ico">{k["ico"]}</div>', None),
        ("That's up to you. Your existing offers, like your student cut, can stay as they are.",
         f"That's up to you. Your existing offers, like your {e(b['offer'])}, can stay as they are." if b['offer']
         else "That's up to you. Any offers you run now can stay as they are.", None),
        ('£30 in sales. You gave £3,', f'{money(spend * 2)} in sales. You gave {money(gave)},', None),
        ('£15.00', money2(spend), None),
        ('£15', money(spend), None),
        ('£510', money(sales), None),
        ('£67.50', money2(cost), None),
        ('data-to="510"', f'data-to="{sales:g}"', None),
        ('data-to="67.5"', f'data-to="{cost:g}"', None),
        ('{ sales: 15,', f'{{ sales: {spend:g},', None),
        ('{ sales: 30,', f'{{ sales: {spend * 2:g},', None),
        ('gave: 3 }', f'gave: {gave:g} }}', None),
    ]
    if b['followers'] >= BIG_IG:
        R.append(('<details><summary>My Instagram is quite small. Does that matter?</summary><p>No. Customers share you with their own friends, on social media or in-person, so you reach people who don\'t follow you yet.</p></details>',
                  '<details><summary>I already have a big following. Why would I need this?</summary><p>Your followers already know you. Cirqle reaches their friends, who don\'t follow you yet, and shows you exactly who each new customer came from.</p></details>', 1))
    for old, new, n in R:
        c = t.count(old)
        if n is not None and c != n:
            raise SystemExit(f'{b["slug"]}{"-story" if story else ""}: expected {n}× {old!r}, found {c}')
        t = t.replace(old, new)
    left = re.findall(r'[Bb]oswell' if k['key'] == 'barber' else r'[Bb]oswell|barber|haircut', t)
    if left:
        raise SystemExit(f'{b["slug"]}: leftover Boswell text {left[:5]}')
    return t


def main():
    brands = json.loads((ROOT / 'scripts/demo_brands.json').read_text())
    masters = {suf: p.read_text() for suf, p in MASTERS.items()}
    built = []
    for b in brands:
        k = kind_of(b['category'])
        b['slug'] = slugify(b['name'])
        b['ini'] = initials(b['name'])
        b['spend'] = b.get('spend') or k['spend'].get(b['price']) or list(k['spend'].values())[0]
        b.setdefault('receipt_item', k['item'])
        b.setdefault('offer', '')
        b['area'] = next(a for rx, a in AREAS if re.search(rx, b['addr_full'], re.I))
        for suf, m in masters.items():
            (ROOT / f'for/{b["slug"]}{suf}.html').write_text(render(m, b, k, bool(suf)))
        built.append((b['name'], b['slug'], k['key']))
    for name, slug, key in built:
        print(f'{slug:40} {key:10} {name}')
    print(len(built), 'brands,', 2 * len(built), 'pages')


if __name__ == '__main__':
    main()
