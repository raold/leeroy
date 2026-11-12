"""Explicit private exports and model-free scoring. Never mutates the source database."""
import json
import argparse
from pathlib import Path
import sys
import time
import uuid
from leeroy_mind.config import load_config
from leeroy_mind.human import human_identity
from leeroy_mind.storage.store import Store
from leeroy_mind.clock import SystemClock
from leeroy_mind.eval.talk.cli import private_logs
from . import dataset,offline_score
from .capture import digest


def add_parsers(commands):
    for name in ('export','evalset'):
        parser=commands.add_parser(name)
        parser.add_argument('--data-dir',default=argparse.SUPPRESS)
        parser.add_argument('--out',type=Path,required=name=='export')
        parser.add_argument('--since',type=int,default=30)
        parser.add_argument('--include-private',action='store_true')
        parser.add_argument('--include-overlay-content',action='store_true')
    parser=commands.add_parser('score')
    parser.add_argument('--data-dir',default=argparse.SUPPRESS)
    parser.add_argument('--evalset',type=Path,required=True)
    parser.add_argument('--candidates',type=Path,required=True)


def run(args,root):
    try:
        with private_logs():
            cfg=load_config(root/'mind.toml')
            scratch=dataset.output_dir(root/'scratch',data_dir=root)
            with dataset.snapshot(root,scratch) as conn:
                store=Store(conn,clock=SystemClock())
                owner=int(store.get_meta('human_guid') or 0)
                if owner<=0:
                    raise ValueError('learn_owner_unavailable')
                ident=human_identity(cfg,store,owner)
                model_hash=digest(cfg.models.embedding)
                if args.action=='score':
                    manifest=json.loads((args.evalset/'manifest.json').read_text(encoding='utf-8'))
                    if manifest.get('kind')!='evalset' or manifest.get('model_hash')!=model_hash:
                        raise ValueError('learn_eval_manifest_invalid')
                    indices=list(offline_score.read_jsonl(args.evalset/'index.jsonl'))
                    result=offline_score.score(conn,indices,offline_score.read_jsonl(args.candidates),owner=owner,
                        model_hash=model_hash,include_private=manifest['include_private'])
                    print(json.dumps(result,allow_nan=False,sort_keys=True))
                    return 0
                if not 1<=args.since<=3650:
                    raise ValueError('learn_days_invalid')
                now=time.time()
                until=now if args.action=='evalset' else now-7*dataset.DAY
                since=now-(7 if args.action=='evalset' else args.since)*dataset.DAY
                target=dataset.output_dir(args.out or scratch/('learn-eval-'+uuid.uuid4().hex[:12]),data_dir=root)
                names=['pairs.jsonl','manifest.json']+(['index.jsonl'] if args.action=='evalset' else [])
                if any((target/name).exists() for name in names):
                    raise ValueError('learn_output_exists')
                if args.include_overlay_content:
                    print('Warning: explicit owner-overlay content may be included in this private dataset.',file=sys.stderr)
                pairs,indices,counts=dataset.mine(conn,ident,since=since,until=until,
                    include_private=args.include_private,include_overlay=args.include_overlay_content)
                if args.action=='evalset':
                    for pair,index in zip(pairs,indices,strict=True):
                        pair['id']=index['id']
                    dataset.write_jsonl(target/'index.jsonl',indices)
                dataset.write_jsonl(target/'pairs.jsonl',pairs)
                manifest={'kind':args.action,'since':since,'until':until,'model_hash':model_hash,
                          'include_private':args.include_private,'include_overlay_content':args.include_overlay_content,
                          **counts}
                dataset.write_jsonl(target/'manifest.json',[manifest])
                print(json.dumps(counts,sort_keys=True))
                return 0
    except Exception as error:
        known={'learn_output_in_repository','learn_output_exists','learn_days_invalid','learn_owner_unavailable',
               'learn_output_linked','learn_member_key_unavailable',
               'learn_eval_manifest_invalid','learn_eval_source_changed','learn_candidate_invalid',
               'learn_candidate_embedding_invalid','learn_input_too_large','learn_input_line_too_large'}
        print(str(error) if type(error) is ValueError and str(error) in known else 'learn_dataset_failed',file=sys.stderr)
        return 1
