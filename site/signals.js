'use strict';
(() => {

  const room=document.querySelector('.pile-group');
  if(!room)return;
  const toggle=document.getElementById('motion-toggle');
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let motionReduced=reduced.matches;
  let userPaused=false,inView=true,suspended=false,raf=0,last=-Infinity,frame=0,seed=29151;
  try{userPaused=sessionStorage.getItem('quickid3-motion-paused')==='true';}catch(_){}
  function random(){seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;return(seed>>>0)/4294967296;}
  // Original 5x7 bitmap lettering: early VCR-style OSD, not a font dependency.
  // It is rasterized into the SAME scan/noise framebuffer as the picture.
  const glyphs={
    'N':['10001','11001','11001','10101','10011','10011','10001'],
    'O':['01110','11011','10001','10001','10001','11011','01110'],
    'S':['01111','11000','10000','01110','00001','00011','11110'],
    'I':['11111','00100','00100','00100','00100','00100','11111'],
    'G':['01110','11000','10000','10111','10001','11001','01111'],
    'A':['01110','11011','10001','11111','10001','10001','10001'],
    'L':['10000','10000','10000','10000','10000','10000','11111'],
    ' ':['00000','00000','00000','00000','00000','00000','00000']
  };
  function letterMask(w,h){
    const text='NO SIGNAL',scale=Math.max(1,Math.floor(w*.64/(text.length*6-1)));
    const mask=new Uint8Array(w*h),x0=Math.round((w-(text.length*6-1)*scale)/2),y0=Math.round(h*.43);
    [...text].forEach((char,n)=>glyphs[char].forEach((row,y)=>[...row].forEach((pixel,x)=>{
      if(pixel==='1')for(let dy=0;dy<scale;dy++)for(let dx=0;dx<scale;dx++)mask[(y0+y*scale+dy)*w+x0+(n*6+x)*scale+dx]=1;
    })));
    return mask;
  }
  const displays=[...document.querySelectorAll('.screen-plane')].map((element,index)=>{
    const canvas=element.querySelector('canvas');let context=null;
    try{context=canvas.getContext('2d');}catch(_){}
    return{element,index,canvas,context,image:context?.createImageData(canvas.width,canvas.height),mask:letterMask(canvas.width,canvas.height)};
  });
  function draw(display,time){
    if(!display.context||display.element.classList.contains('has-picture'))return;
    const{canvas,context,image,mask,index}=display,w=canvas.width,h=canvas.height;
    const phase=time/1000;const rolling=((phase*12+index*37)%(h+40))-20;
    for(let y=0;y<h;y++){
      const scan=y%3===0?.60:1;
      const inBand=Math.abs(y-rolling)<7;
      const shift=Math.round(Math.sin(y*.09+phase*.67+index)*.7+(inBand?Math.sin(phase*3+index)*4:0));
      for(let x=0;x<w;x++){
        const offset=(y*w+x)*4,sourceX=x+shift;
        const glyph=sourceX>=0&&sourceX<w&&mask[y*w+sourceX];
        const grain=random();
        let value=glyph?194+grain*50:18+grain*83;
        if(glyph&&grain<.075)value=52+grain*100;
        const edge=Math.max(.48,1-((x/w-.5)**2+(y/h-.5)**2)*1.0);
        value*=scan*edge*(inBand?.72:1);
        image.data[offset]=value;image.data[offset+1]=value;image.data[offset+2]=value;image.data[offset+3]=255;
      }
    }
    context.putImageData(image,0,0);display.element.classList.add('rendered');
  }
  function shouldRun(){return !userPaused&&!motionReduced&&!document.hidden&&inView&&!suspended;}
  function tick(now){raf=0;if(!shouldRun())return;if(now-last>=100){displays.forEach(d=>draw(d,frame*100));last=now;frame++;}raf=requestAnimationFrame(tick);}
  function sync(){
    const paused=userPaused||motionReduced;document.body.dataset.motion=paused?'paused':'running';
    toggle.disabled=motionReduced;toggle.setAttribute('aria-pressed',String(paused));
    toggle.textContent=motionReduced?'Motion reduced':paused?'Resume motion':'Pause motion';
    if(shouldRun()&&!raf){last=-Infinity;raf=requestAnimationFrame(tick);}else if(!shouldRun()&&raf){cancelAnimationFrame(raf);raf=0;}
  }
  displays.forEach(d=>draw(d,0));
  toggle.addEventListener('click',()=>{userPaused=!userPaused;try{sessionStorage.setItem('quickid3-motion-paused',String(userPaused));}catch(_){}window.dispatchEvent(new CustomEvent('quickid3:motion',{detail:{paused:userPaused}}));sync();});
  window.addEventListener('quickid3:motion',event=>{userPaused=Boolean(event.detail.paused);sync();});
  reduced.addEventListener('change',event=>{motionReduced=event.matches;sync();});
  document.addEventListener('visibilitychange',sync);
  const observer='IntersectionObserver'in window?new IntersectionObserver(entries=>{inView=entries[0].isIntersecting;sync();},{rootMargin:'60px'}):null;
  observer?.observe(room);
  window.addEventListener('pagehide',()=>{suspended=true;sync();});
  window.addEventListener('pageshow',()=>{suspended=false;sync();});
  sync();
})();
