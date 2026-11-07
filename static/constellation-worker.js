/* Bounded spatial-grid layout. Springs position evidence; they never create it. */
onmessage=({data})=>{
  const ns=data.nodes.map(n=>({...n,vx:0,vy:0})),by=new Map(ns.map(n=>[n.id,n]));
  const es=data.edges.map(e=>[by.get(e.source),by.get(e.target),e.strength||1]).filter(([a,b])=>a&&b&&a!==b),cell=data.overview?160:50;
  for(let tick=0;tick<160;tick++){
    const grid=new Map(),heat=1-tick/185;
    for(const n of ns){const k=Math.floor(n.x/cell)+','+Math.floor(n.y/cell);if(!grid.has(k))grid.set(k,[]);grid.get(k).push(n);}
    for(const n of ns){const gx=Math.floor(n.x/cell),gy=Math.floor(n.y/cell);let checks=0;
      neighbors:for(let x=gx-1;x<=gx+1;x++)for(let y=gy-1;y<=gy+1;y++)for(const o of grid.get(x+','+y)||[]){if(checks++>80)break neighbors;if(o===n)continue;let dx=n.x-o.x,dy=n.y-o.y,d2=dx*dx+dy*dy;if(d2<.01){dx=.1;dy=.1;d2=.02;}const f=Math.min(2,(data.overview?500:90)/d2)*heat;n.vx+=dx*f*.12;n.vy+=dy*f*.12;}
      if(n.anchor){n.vx+=(n.anchor.x-n.x)*.002;n.vy+=(n.anchor.y-n.y)*.002;}
    }
    for(const [a,b,strength] of es){const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,same=a.anchor&&b.anchor&&a.anchor.x===b.anchor.x&&a.anchor.y===b.anchor.y,desired=data.overview?230:same?35:280,f=(d-desired)*.006*heat*strength;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;}
    for(const n of ns){n.vx*=.62;n.vy*=.62;n.x+=Math.max(-5,Math.min(5,n.vx));n.y+=Math.max(-5,Math.min(5,n.vy));}
    if(tick%16===0||tick===159)postMessage({positions:ns.map(({id,x,y})=>({id,x,y})),done:tick===159});
  }
};
