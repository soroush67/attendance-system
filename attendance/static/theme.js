(function(){
  const root=document.documentElement;
  function getSaved(){try{return localStorage.getItem('attendance-theme')}catch(e){return null}}
  function save(theme){try{localStorage.setItem('attendance-theme',theme)}catch(e){}}
  function setTheme(theme){
    root.setAttribute('data-theme',theme);
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      const dark=theme==='dark';
      btn.setAttribute('aria-label',dark?'فعال کردن حالت روز':'فعال کردن حالت شب');
      btn.setAttribute('aria-pressed',String(dark));
      btn.querySelector('.theme-symbol').textContent=dark?'☀':'☾';
      btn.querySelector('.theme-label').textContent=dark?'حالت روز':'حالت شب';
    });
  }
  document.addEventListener('DOMContentLoaded',function(){
    setTheme(root.getAttribute('data-theme')||'light');
    document.querySelectorAll('[data-theme-toggle]').forEach(function(btn){
      btn.addEventListener('click',function(){const next=root.getAttribute('data-theme')==='dark'?'light':'dark';save(next);setTheme(next)});
    });
  });
})();
