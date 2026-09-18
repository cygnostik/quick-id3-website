'use strict';
(() => {
  const footer=document.querySelector('.glass-footer'),canvas=footer?.querySelector('.footer-snow'),button=footer?.querySelector('.footer-motion');
  if(!canvas||!button)return;
  let ctx;try{ctx=canvas.getContext('2d');}catch(_){return;}if(!ctx)return;
  canvas.width=256;canvas.height=80;
  const image=ctx.createImageData(canvas.width,canvas.height),media=matchMedia('(prefers-reduced-motion: reduce)');
  let reduced=media.matches,paused=false,visible=false,suspended=false,raf=0,last=-Infinity,frame=0,seed=73849;
  try{paused=sessionStorage.getItem('quickid3-motion-paused')==='true';}catch(_){}
  function draw(){
    for(let y=0;y<canvas.height;y++)for(let x=0;x<canvas.width;x++){
      seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;
      const grain=(seed>>>0)/4294967296,roll=Math.sin((y+frame*.4)*.19)*8,v=80+grain*86+roll,i=(y*canvas.width+x)*4;
      image.data[i]=v;image.data[i+1]=v;image.data[i+2]=v;image.data[i+3]=255;
    }
    ctx.putImageData(image,0,0);frame++;
  }
  function running(){return !paused&&!reduced&&visible&&!document.hidden&&!suspended;}
  function tick(now){raf=0;if(!running())return;if(now-last>=140){draw();last=now;}raf=requestAnimationFrame(tick);}
  function sync(){
    button.hidden=false;button.disabled=reduced;button.setAttribute('aria-pressed',String(paused||reduced));button.textContent=reduced?'Motion reduced':paused?'Resume effects':'Pause effects';
    footer.dataset.motion=paused||reduced?'paused':'running';
    if(running()&&!raf){last=-Infinity;raf=requestAnimationFrame(tick);}else if(!running()&&raf){cancelAnimationFrame(raf);raf=0;}
  }
  button.addEventListener('click',()=>{paused=!paused;try{sessionStorage.setItem('quickid3-motion-paused',String(paused));}catch(_){}window.dispatchEvent(new CustomEvent('quickid3:motion',{detail:{paused}}));sync();});
  window.addEventListener('quickid3:motion',event=>{paused=Boolean(event.detail.paused);sync();});
  media.addEventListener('change',event=>{reduced=event.matches;sync();});
  document.addEventListener('visibilitychange',sync);
  window.addEventListener('pagehide',()=>{suspended=true;sync();});
  window.addEventListener('pageshow',()=>{suspended=false;sync();});
  const observer='IntersectionObserver'in window?new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();}):null;
  if(observer)observer.observe(footer);else visible=true;
  draw();sync();
})();
