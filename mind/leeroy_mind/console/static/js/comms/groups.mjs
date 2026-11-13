import { el, emit } from "../core.mjs";
import { face } from "../faces.mjs";
import { DISCORD, discordReason, discordBusy, mutateDiscord } from "./discordstate.mjs";
import { openView } from "./feedview.mjs";

function button(text, action) {
  const b = el('button', '', text); b.type = 'button'; b.addEventListener('click', action); return b;
}
export function currentGroup(view) { return DISCORD.groups.find(g => g.id === view); }
export function groupReason(view) { return currentGroup(view)?.closed != null ? 'This group is closed. Its history is still readable.' : ''; }
export function groupList(root, channelButton) {
  if (!DISCORD.groups_enabled) { return; }
  root.append(el('h4', 'sec', 'Group chats'));
  const create = button('New group', () => groupDialog());
  create.disabled = !!discordReason() || discordBusy; create.dataset.focus = 'group-new'; root.append(create);
  for (const g of DISCORD.groups) {
    const b = channelButton(g.id, g.name + (g.closed != null ? ' · closed' : ''), false);
    const stack = el('span','group-faces'); stack.setAttribute('aria-hidden','true');
    for (const guid of g.members.slice(0,3)) { stack.append(face(guid,DISCORD.name(guid),'list')); }
    b.prepend(stack); root.append(b);
  }
}
export function groupPanel(root, view) {
  const group = currentGroup(view); if (!group) { return false; }
  root.append(el('h3','',group.name),el('p','hint','You and these members'));
  for (const guid of group.members) {
    const row = el('div','group-member'); row.append(face(guid,DISCORD.name(guid),'list'),el('span','',DISCORD.name(guid)));
    const remove = button('Remove',() => groupDialog(group,'remove',guid));
    remove.disabled = !!groupReason(view) || !!discordReason() || discordBusy; row.append(remove); root.append(row);
  }
  if (group.closed != null) { root.append(el('p','hint',groupReason(view))); return true; }
  const manage = button('Add members or rename',() => groupDialog(group));
  const leave = button('Leave group',() => groupDialog(group,'leave'));
  manage.disabled = leave.disabled = !!discordReason() || discordBusy; root.append(manage,leave); return true;
}
function groupDialog(group = null, mode = 'edit', guid = null) {
  const prior = document.activeElement, dialog = el('dialog','group-dialog');
  const title = el('h3','',mode === 'leave' ? 'Leave and close this group?' : mode === 'remove' ? 'Remove '+DISCORD.name(guid)+'?' : group ? 'Group members and name' : 'New group');
  title.id = 'group-dialog-title'; dialog.setAttribute('aria-labelledby',title.id);
  const form = el('form'), error = el('p','bad'); error.setAttribute('role','alert');
  const label = el('label','','Group name (optional, up to 40 characters)'), name = el('input');
  name.id='group-dialog-name'; name.maxLength=40; name.value=group?.name || ''; label.htmlFor=name.id;
  const choices = el('fieldset'), legend=el('legend','',group ? 'Add members (four besides you at most)' : 'Choose two to four members'); choices.append(legend);
  const boxes=[];
  for (const member of DISCORD.dms.filter(m => !group?.members.includes(m.guid))) {
    const row=el('label','group-choice'), input=el('input'); input.type='checkbox'; input.value=member.guid;
    row.append(input,document.createTextNode(member.name)); choices.append(row); boxes.push(input);
  }
  form.append(title);
  if (mode==='edit') { form.append(label,name,choices); }
  else { form.append(el('p','',mode==='leave' ? 'The history stays readable. New messages stop.' : 'Removing a member leaves the history intact. Fewer than two members closes the group.')); }
  const cancel=button('Cancel',() => dialog.close()), submit=el('button','primary',mode==='leave' ? 'Leave group' : mode==='remove' ? 'Remove member' : 'Save');
  submit.type='submit'; const actions=el('div','acts'); actions.append(cancel,submit); form.append(error,actions); dialog.append(form);
  form.addEventListener('submit',async event => {
    event.preventDefault(); error.textContent='';
    const picked=boxes.filter(b=>b.checked).map(b=>Number(b.value));
    if (mode==='edit' && (!group && (picked.length<2 || picked.length>4) || group && picked.length+group.members.length>4)) {
      error.textContent='Choose two to four members besides you.'; return;
    }
    submit.disabled=true;
    try {
      if (mode==='leave' || mode==='remove') {
        await mutateDiscord({action:mode==='leave'?'group_leave':'group_remove',channel:group.id,...(guid?{guid}:{})});
      } else if (!group) {
        const result=await mutateDiscord({action:'group_create',members:picked,name:name.value});
        if (result.channel) { openView(result.channel); }
      } else {
        for (const member of picked) { await mutateDiscord({action:'group_add',channel:group.id,guid:member}); }
        if (name.value!==group.name) { await mutateDiscord({action:'group_rename',channel:group.id,name:name.value}); }
      }
      dialog.close();
    } catch (e) { error.textContent=e.message; } finally { submit.disabled=false; }
  });
  dialog.addEventListener('close',() => { dialog.remove(); if (prior?.isConnected) { prior.focus(); } else { emit('discord-view'); } });
  document.body.append(dialog); dialog.showModal(); (mode==='edit'?name:cancel).focus();
}
