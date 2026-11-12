import test from "node:test";
import assert from "node:assert/strict";

// Minimal stand-in DOM, exercising the actual gallery and tab modules without a server.
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.attrs={};
    this.events={}; this.textContent=""; this.hidden=false;
  }
  get firstChild() { return this.children[0]; }
  appendChild(e) { this.children.push(e); e.parentNode=this; return e; }
  append(...es) { es.forEach(e=>this.appendChild(e)); }
  prepend(e) { this.children.unshift(e); e.parentNode=this; }
  removeChild(e) { this.children.splice(this.children.indexOf(e),1); e.parentNode=null; }
  remove() { this.parentNode.removeChild(this); }
  setAttribute(k,v) { this.attrs[k]=String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k,fn) { this.events[k]=fn; }
  focus() { document.activeElement=this; }
  contains(e) { return e===this || this.children.some(c=>c.contains(e)); }
  showModal() {}
  close() { this.events.close(); }
  scrollIntoView() {}
}
const body=new Element("body"), tabs=body.appendChild(new Element("nav"));
function find(test,node=body) {
  if (test(node)) return node;
  for (const c of node.children) { const match=find(test,c); if(match) return match; }
}
globalThis.document={body,activeElement:null,hidden:false,
  createElement:t=>new Element(t),getElementById:id=>find(e=>e.id===id),
  querySelector:()=>tabs,addEventListener() {}};
const {emit}=await import("../../../leeroy_mind/console/static/js/core.mjs");
const {S}=await import("../../../leeroy_mind/console/static/js/state.mjs");
const {openGallery}=await import("../../../leeroy_mind/console/static/js/gallery.mjs");
const {banner,pollTab}=await import("../../../leeroy_mind/console/static/js/tabkit.mjs");
// Only freshness's periodic redraw is suppressed; promises and request deadlines are real.
const interval=globalThis.setInterval;
globalThis.setInterval=()=>0;
await import("../../../leeroy_mind/console/static/js/freshness.mjs");
globalThis.setInterval=interval;
async function settle() { for(let i=0;i<40;i++) await Promise.resolve(); }

test("gallery refresh preserves the focused photo when its index changes",async()=>{
  const opener=body.appendChild(new Element("button")); opener.isConnected=true;
  let photos=[{id:"current",rendered:3,cover:true},{id:"focused",rendered:2},{id:"older",rendered:1}];
  globalThis.fetch=async()=>({status:200,json:async()=>({view:{photos}})});
  openGallery(101,"Arkon",opener);
  await settle();
  const strip=find(e=>e.className==="gallery-strip");
  strip.children[1].focus();
  photos=[{id:"new",rendered:4},...photos];
  emit("status",{});
  await settle();
  assert.equal(document.activeElement,strip.children[2]);
  assert.equal(strip.children[1].getAttribute("aria-current"),"true");
  find(e=>e.tagName==="DIALOG").close();
  assert.equal(document.activeElement,opener);
});

test("poll failure shows server words once and retains retry status",async()=>{
  const panel=body.appendChild(new Element("section")); panel.id="tab-guild";
  const notice=panel.appendChild(new Element("p")); notice.id="guild-banner";
  S.tab="guild";
  globalThis.fetch=async()=>new Response(JSON.stringify({error:"Guild source unavailable"}),
    {status:503,headers:{"content-type":"application/json"}});
  const once=pollTab("guild",{path:"/api/guild",ms:1000,render(){}});
  once();
  // Response body decoding spans stream microtasks.
  await new Promise(resolve=>setImmediate(resolve));
  await settle();
  const stale=document.getElementById("guild-stale");
  assert.equal(notice.textContent,"Guild source unavailable");
  assert.ok(stale.textContent.includes("Retrying"));
  assert.ok(!stale.textContent.includes("Guild source unavailable"));
  banner("guild",null);
  assert.ok(stale.textContent.includes("Guild source unavailable"));
  emit("request-health",{path:"/api/guild",ok:true,error:""});
  assert.equal(stale.hidden,true);
});
