(function () {
  var button = document.querySelector('.nav-button');
  var nav = document.getElementById('site-nav');

  if (!button || !nav) {
    return;
  }

  function closeMenu() {
    button.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
  }

  function openMenu() {
    button.classList.add('is-open');
    button.setAttribute('aria-expanded', 'true');
  }

  button.addEventListener('click', function () {
    if (button.classList.contains('is-open')) {
      closeMenu();
    } else {
      openMenu();
    }
  });

  nav.addEventListener('click', function (event) {
    if (event.target.tagName === 'A') {
      closeMenu();
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeMenu();
    }
  });
})();
