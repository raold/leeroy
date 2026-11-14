import { $, clear, el } from "./core.mjs";
import { face } from "./faces.mjs";
import { pollTab } from "./tabkit.mjs";
import { SECTIONS, INTRO, cardModel } from "./tonightmodel.mjs";

function renderTonight(value) {
  $('tonight-intro').textContent = INTRO;
  const box = $('tonight-cards');
  clear(box);
  SECTIONS.forEach(name => {
    const model = cardModel(name,value.sections?.[name],value.sections?.outreach), card = el('article','panel');
    const header = el('header',''); header.appendChild(el('h3','',model.title)); card.appendChild(header);
    model.lines.forEach(line => {
      const r = el('p','tonight-line');
      line.people.forEach(p => {
        r.appendChild(face(p.guid,p.name,'list'));
        r.appendChild(el('span','',p.name));
      });
      const text = el(line.href ? 'a' : 'span','',line.text);
      if (line.href) { text.href = line.href; }
      r.appendChild(text); card.appendChild(r);
    });
    box.appendChild(card);
  });
}
pollTab('tonight',{path:'/api/tonight',ms:60000,render:renderTonight});
