'use strict';
// Four app-image inputs shared by the home pile and full screenshot page.
// The two permanent snow displays are deliberately excluded.
const SCREENSHOTS = {
  'windows-crt': 'assets/screenshots/windows-selection.png',
  'mac-crt': 'assets/screenshots/mac-selection.png',
  'windows-lcd': 'assets/screenshots/windows-inline-edit.png',
  'mac-lcd': 'assets/screenshots/mac-inline-edit.png'
};
(() => {
  const SVG='http://www.w3.org/2000/svg';
  Object.entries(SCREENSHOTS).forEach(([slot,url])=>{
    if(!url)return;
    const probe=new Image();
    probe.onload=()=>{
      const plane=document.querySelector(`[data-slot="${slot}"]`);
      if(plane){
        const bounds=plane.querySelector('foreignObject');
        const image=document.createElementNS(SVG,'image');
        ['x','y','width','height'].forEach(name=>image.setAttribute(name,bounds.getAttribute(name)));
        image.setAttribute('href',url);image.setAttribute('preserveAspectRatio','xMidYMid meet');
        plane.append(image);plane.classList.add('has-picture');
      }
      const panel=document.querySelector(`[data-screenshot="${slot}"]`);
      if(panel&&!panel.querySelector('img')){
        const image=probe.cloneNode();image.alt=`Quick·ID3 on ${slot.startsWith('windows')?'Windows':'Mac'}`;
        panel.replaceChildren(image);
      }
    };
    probe.onerror=()=>{document.querySelector(`[data-screenshot="${slot}"]`)?.setAttribute('data-image-error','true');};
    probe.src=url;
  });
})();
