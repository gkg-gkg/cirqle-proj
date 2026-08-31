/* ── Cirqle shared UI behaviours (loaded site-wide) ──
   Self-contained & idempotent: injects its own elements, so no
   per-page markup is required. Heavy effects are skipped on touch
   devices and when the user prefers reduced motion. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── Scroll progress bar ──
  var bar = document.getElementById('scrollProgress');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'scroll-progress';
    bar.id = 'scrollProgress';
    document.body.appendChild(bar);
  }
  function onScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    bar.style.width = (max > 0 ? (window.scrollY / max) * 100 : 0) + '%';
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ── Nav: hairline + shadow once the page scrolls ──
  var nav = document.querySelector('.nav');
  if (nav) {
    var onNavScroll = function () {
      nav.classList.toggle('scrolled', window.scrollY > 8);
    };
    window.addEventListener('scroll', onNavScroll, { passive: true });
    onNavScroll();
  }

  // ── Current page + session ──
  var here = location.pathname.split('/').pop() || 'index.html';
  var session = null;
  try {
    session = JSON.parse(
      localStorage.getItem('cirqle_session') ||
      sessionStorage.getItem('cirqle_session') || 'null'
    );
  } catch (e) { /* ignore malformed session */ }

  // ── Consistent top banner across the main site ──
  // Centre = Browse Deals + For brands; right = My Account / Dashboard chips.
  // The chip for the page you're on is hidden, and there's no sign-out chip
  // (sign-out lives on the account page). Skipped on the auth pages + merchant
  // portal, which keep their own navs.
  var SPECIAL_NAV = { 'signin.html': 1, 'signup.html': 1, 'merchant.html': 1, 'reset-password.html': 1 };
  if (nav && !SPECIAL_NAV[here]) {
    var navLinks = nav.querySelector('.nav-links');
    if (!navLinks) {
      navLinks = document.createElement('ul');
      navLinks.className = 'nav-links';
      var logoEl = nav.querySelector('.logo');
      if (logoEl) logoEl.insertAdjacentElement('afterend', navLinks);
      else nav.insertBefore(navLinks, nav.firstChild);
    }
    navLinks.innerHTML =
      '<li><a href="browse.html">Browse Deals</a></li>' +
      '<li><a href="for-brands.html">For brands</a></li>';

    var cta = nav.querySelector('.nav-cta');
    if (!cta) { cta = document.createElement('div'); cta.className = 'nav-cta'; nav.appendChild(cta); }
    // Pages with their own hand-built avatar+name block (feed/dashboard/account/
    // receipt) own their .nav-cta already — don't blow it away from under them.
    if (!cta.querySelector('.nav-user')) {
      var acctHref = session ? 'dashboard.html' : 'signin.html';
      var dashHref = session ? 'feed.html' : 'signin.html';
      var chips = '';
      if (here !== 'dashboard.html') chips += '<a href="' + acctHref + '" class="btn btn-sm">My Account</a>';
      if (here !== 'feed.html')      chips += '<a href="' + dashHref + '" class="btn btn-sm btn-ink">Dashboard</a>';
      cta.innerHTML = chips;
    }
  }

  // ── Mark the current page's centre link ──
  document.querySelectorAll('.nav-links a').forEach(function (a) {
    if ((a.getAttribute('href') || '').split(/[?#]/)[0] === here) a.setAttribute('aria-current', 'page');
  });

  // ── Auth-aware nav (for pages keeping their own data-auth chips) ──
  document.querySelectorAll('[data-auth="out"]').forEach(function (el) {
    if (session) el.style.display = 'none';
  });
  document.querySelectorAll('[data-auth="in"]').forEach(function (el) {
    el.style.display = session ? '' : 'none';
  });

  // ── Mobile menu: burger + dropdown injected into the nav ──
  // .nav-links is hidden under 900px, so phones get a proper menu with the
  // core site links plus auth-aware account links. Idempotent + self-contained.
  if (nav && !document.getElementById('navBurger')) {
    var burger = document.createElement('button');
    burger.className = 'nav-burger';
    burger.id = 'navBurger';
    burger.type = 'button';
    burger.setAttribute('aria-label', 'Open menu');
    burger.setAttribute('aria-expanded', 'false');
    burger.innerHTML = '<span></span>';
    nav.appendChild(burger);

    var menu = document.createElement('div');
    menu.className = 'nav-menu';
    menu.id = 'navMenu';
    var links = [
      ['browse.html', 'Browse Deals'],
      ['for-brands.html', 'For brands'],
      ['about.html', 'About'],
      ['help.html', 'Help centre'],
    ];
    var authLinks = session
      ? [['dashboard.html', 'My Account'], ['feed.html', 'Dashboard'], ['account.html', 'Account details']]
      : [['signin.html', 'Sign in'], ['signup.html', 'Sign up']];
    menu.innerHTML =
      links.map(function (l) { return '<a href="' + l[0] + '">' + l[1] + '</a>'; }).join('') +
      '<div class="nav-menu-divider"></div>' +
      authLinks.map(function (l) { return '<a href="' + l[0] + '">' + l[1] + '</a>'; }).join('') +
      (session ? '<a href="#" id="menuSignOut">Sign out</a>' : '');
    document.body.appendChild(menu);

    var so = document.getElementById('menuSignOut');
    if (so) {
      so.addEventListener('click', function (e) {
        e.preventDefault();
        localStorage.removeItem('cirqle_session');
        sessionStorage.removeItem('cirqle_session');
        window.location.href = 'index.html';
      });
    }

    // mark the current page in the menu
    menu.querySelectorAll('a').forEach(function (a) {
      if ((a.getAttribute('href') || '').split(/[?#]/)[0] === here) {
        a.setAttribute('aria-current', 'page');
      }
    });

    var closeMenu = function () {
      menu.classList.remove('open');
      burger.classList.remove('open');
      burger.setAttribute('aria-expanded', 'false');
    };
    burger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = menu.classList.toggle('open');
      burger.classList.toggle('open', open);
      burger.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function (e) {
      if (menu.classList.contains('open') && !menu.contains(e.target)) closeMenu();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenu();
    });
  }

  // ── Scroll reveal ──
  var revealEls = document.querySelectorAll('.reveal');
  if (revealEls.length) {
    if (reduceMotion || !('IntersectionObserver' in window)) {
      revealEls.forEach(function (el) { el.classList.add('visible'); });
    } else {
      var obs = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) { e.target.classList.add('visible'); obs.unobserve(e.target); }
        });
      }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
      revealEls.forEach(function (el) { obs.observe(el); });
    }
  }

  // ── Back to top — floating ring, appears after two screens ──
  var topBtn = document.getElementById('backTop');
  if (!topBtn) {
    topBtn = document.createElement('button');
    topBtn.className = 'back-top';
    topBtn.id = 'backTop';
    topBtn.type = 'button';
    topBtn.setAttribute('aria-label', 'Back to top');
    topBtn.innerHTML = '&#8593;';
    document.body.appendChild(topBtn);
    topBtn.addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    });
  }
  var onTopScroll = function () {
    topBtn.classList.toggle('show', window.scrollY > window.innerHeight * 1.5);
  };
  window.addEventListener('scroll', onTopScroll, { passive: true });
  onTopScroll();

  // ── Global toast helper ──
  window.cirqleToast = function (msg) {
    var t = document.getElementById('toast');
    if (!t) {
      t = document.createElement('div');
      t.className = 'toast';
      t.id = 'toast';
      t.setAttribute('role', 'status');
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.classList.remove('show'); }, 3000);
  };

  // ── Pointer-driven polish (skip on touch / reduced motion) ──
  var finePointer = window.matchMedia('(pointer: fine)').matches;
  if (!finePointer || reduceMotion) return;

  // Card spotlight: feed cursor position to CSS (--mx / --my) so the
  // wash + lit border ring in cirqle.css track the pointer. Delegated
  // so cards injected later (deals grids, feeds) work automatically.
  document.addEventListener('pointermove', function (e) {
    var el = e.target && e.target.closest
      ? e.target.closest('.card-hover, .spot')
      : null;
    if (!el) return;
    var r = el.getBoundingClientRect();
    el.style.setProperty('--mx', (e.clientX - r.left) + 'px');
    el.style.setProperty('--my', (e.clientY - r.top) + 'px');
  }, { passive: true });

  // Sticker lift on hover
  document.querySelectorAll('.sticker').forEach(function (s) {
    s.addEventListener('mouseenter', function () {
      s.style.transition = 'transform .3s cubic-bezier(.34,1.56,.64,1)';
      s.style.transform = 'scale(1.06)';
    });
    s.addEventListener('mouseleave', function () { s.style.transform = ''; });
  });

  // Coin flip on hover
  document.querySelectorAll('.coin').forEach(function (c) {
    c.addEventListener('mouseenter', function () {
      c.style.transition = 'transform .5s cubic-bezier(.34,1.56,.64,1)';
      c.style.transform = 'rotateY(360deg)';
      setTimeout(function () { c.style.transform = ''; }, 520);
    });
  });
})();
