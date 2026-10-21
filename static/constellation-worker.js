/* Bounded grid-based repulsion: no all-pairs simulation, no network access. */
onmessage=({data})=>{
  const ns=data.nodes.map(n=>({...n,vx:0,vy:0})), by=new Map(ns.map(n=>[n.id,n]));
  const fixed=new Set(data.fixedIds||[]);fixed.add(data.fixed);
  const es=data.edges.map(e=>[by.get(e.source),by.get(e.target)]).filter(([a,b])=>a&&b&&a!==b);
  for(let tick=0;tick<180;tick++){
    const grid=new Map(),cell=55,heat=1-tick/210;
    for(const n of ns){const key=Math.floor(n.x/cell)+','+Math.floor(n.y/cell);if(!grid.has(key))grid.set(key,[]);grid.get(key).push(n);}
    for(const n of ns){const gx=Math.floor(n.x/cell),gy=Math.floor(n.y/cell);let checks=0;
      neighbors:for(let x=gx-1;x<=gx+1;x++)for(let y=gy-1;y<=gy+1;y++)for(const o of grid.get(x+','+y)||[]){if(checks++>80)break neighbors;if(o===n)continue;let dx=n.x-o.x,dy=n.y-o.y,d2=dx*dx+dy*dy;if(d2<.01){dx=.1;dy=.1;d2=.02;}const f=Math.min(1.5,65/d2)*heat;n.vx+=dx*f*.1;n.vy+=dy*f*.1;}
      n.vx-=n.x*.00012;n.vy-=n.y*.00012;
    }
    for(const [a,b] of es){const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,f=(d-27)*.012*heat;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;}
    for(const n of ns){if(fixed.has(n.id)){n.vx=0;n.vy=0;continue;}n.vx*=.62;n.vy*=.62;n.x+=Math.max(-5,Math.min(5,n.vx));n.y+=Math.max(-5,Math.min(5,n.vy));}
    if(tick%12===0||tick===179)postMessage({positions:ns.map(({id,x,y})=>({id,x,y})),done:tick===179});
  }
};
