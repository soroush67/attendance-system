// Runs in <head> before first paint: no light/dark flash. External file so CSP can forbid inline scripts.
(function(){try{var t=localStorage.getItem('attendance-theme');document.documentElement.setAttribute('data-theme',t==='light'||t==='dark'?t:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'))}catch(e){document.documentElement.setAttribute('data-theme','light')}})();
