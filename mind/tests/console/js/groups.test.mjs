import test from 'node:test';
import assert from 'node:assert/strict';
import {Discord,channelOf,viewOfChannel} from '../../../leeroy_mind/console/static/js/comms/discord.mjs';

test('group view ids and read marks remain separate from pairs and game groups',()=>{
  assert.equal(channelOf('gdm:12'),'gdm:12'); assert.equal(viewOfChannel('gdm:12'),'gdm:12');
  for (const id of ['gdm:0','gdm:-1','gdm:1x','group']) { assert.equal(channelOf(id),''); }
  const model=new Discord({'gdm:12':8});
  model.adopt({groups_enabled:true,groups:[{id:'gdm:12',name:'Fixture friends',members:[101,102],last_id:10,unread:2,closed:123}]});
  assert.equal(model.title('gdm:12'),'Fixture friends'); assert.equal(model.unread('gdm:12'),2);
  model.markRead('gdm:12'); assert.equal(model.marks()['gdm:12'],10); assert.equal(model.unread('gdm:12'),0);
  model.adopt({groups_enabled:false,groups:[{id:'gdm:12'}]}); assert.deepEqual(model.groups,[]);
});
