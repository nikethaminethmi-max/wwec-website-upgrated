(function () {
  // Theme toggle
  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var dark = document.documentElement.classList.toggle('dark');
      try { localStorage.setItem('color-theme', dark ? 'dark' : 'light'); } catch (e) {}
    });
  }

  // Mobile menu
  var menuBtn = document.getElementById('menu-btn');
  var menu = document.getElementById('mobile-menu');
  if (menuBtn && menu) {
    menuBtn.addEventListener('click', function () {
      var open = menu.classList.toggle('hidden') === false;
      menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // Gallery filter
  var buttons = document.querySelectorAll('.filter-btn');
  var cards = document.querySelectorAll('.project-card');
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      buttons.forEach(function (b) { b.classList.remove('is-active'); });
      btn.classList.add('is-active');
      var f = btn.getAttribute('data-filter');
      cards.forEach(function (c) {
        var show = f === 'all' || c.getAttribute('data-category') === f;
        c.classList.toggle('hidden', !show);
      });
    });
  });

  // Contact form: sends the enquiry to the backend (/api/contact)
  var form = document.getElementById('consultationForm');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var fb = document.getElementById('formFeedback');
      var btn = form.querySelector('button[type="submit"]');
      var okText = fb ? fb.textContent : '';
      var val = function (id) { var el = document.getElementById(id); return el ? el.value : ''; };
      var show = function (msg, ok) {
        if (!fb) return;
        fb.textContent = msg;
        fb.classList.remove('hidden');
        fb.classList.toggle('bg-mint', ok);
        fb.classList.toggle('bg-red-100', !ok);
        fb.classList.toggle('text-red-800', !ok);
      };
      var origBtnText = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Sending...';
      fetch('/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: val('n'),
          phone: val('p'),
          email: val('e'),
          service: val('s'),
          subject: val('sub'),
          message: val('m'),
          website: val('website')
        })
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (res.ok) {
            form.reset();
            show(res.d.message || okText || 'Thank you! We will contact you shortly.', true);
          } else {
            show(res.d.error || Object.values(res.d.errors || {}).join(' ') || 'Something went wrong. Please try again.', false);
          }
        })
        .catch(function () { show('Could not reach the server. Please try again or call us.', false); })
        .finally(function () {
          btn.disabled = false;
          btn.textContent = origBtnText;
        });
    });
  }

  // Scroll reveal
  var items = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  } else {
    items.forEach(function (el) { el.classList.add('in'); });
  }

  // Counting animation: runs once per element, 1 -> data-count, then shows "26+"
  var counters = document.querySelectorAll('[data-count]');
  if (counters.length) {
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    var finalText = function (el) {
      return el.getAttribute('data-count') + (el.getAttribute('data-suffix') || '');
    };
    var ease = function (t) { return 0.5 - Math.cos(Math.PI * t) / 2; }; // easeInOutSine: smooth, never skips a number

    var runCounter = function (el) {
      if (el.getAttribute('data-counted')) return; // never restart
      el.setAttribute('data-counted', '1');

      var target = parseInt(el.getAttribute('data-count'), 10);
      var duration = 2400;
      var startTime = null;

      var step = function (now) {
        if (startTime === null) startTime = now;
        var p = Math.min((now - startTime) / duration, 1);
        var value = Math.round(1 + (target - 1) * ease(p));
        if (value >= target || p >= 1) {
          el.textContent = finalText(el);  // "26+" and stop
          el.classList.add('count-done');  // subtle finish animation
          return;
        }
        el.textContent = value;
        window.requestAnimationFrame(step);
      };
      window.requestAnimationFrame(step);
    };

    if (reduceMotion) {
      counters.forEach(function (el) { el.textContent = finalText(el); });
    } else {
      var counterIO = ('IntersectionObserver' in window) ? new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) { runCounter(en.target); counterIO.unobserve(en.target); }
        });
      }, { threshold: 0.5 }) : null;

      counters.forEach(function (el) {
        el.textContent = '1'; // start from 1
        if (el.getAttribute('data-count-on') === 'load' || !counterIO) {
          runCounter(el);       // hero badge starts on page load
        } else {
          counterIO.observe(el); // stats section starts when visible
        }
      });
    }
  }
})();
