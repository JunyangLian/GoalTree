"""Initialize a derived cross-batch budget ledger without losing original records."""
import argparse
import hashlib
import json
from pathlib import Path


def prepare(output):
    if (output/'runs').exists():raise ValueError('Budget import is only allowed before any formal slot starts')
    ledger=output/'budget-ledger.jsonl';raw=ledger.read_bytes()
    backup=output/'budget-ledger-import-raw.jsonl'
    with backup.open('xb') as stream:stream.write(raw)
    rows=[json.loads(line) for line in raw.decode('utf-8').splitlines()]
    for row in rows:
        row['source_id']=row['id'];row['id']='carried-prior-batches::'+row['id']
    data=''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows).encode()
    ledger.write_bytes(data)
    proof={'scope':'Before-first-run initialization of derived cost ledger; raw prior ledger preserved byte-for-byte',
        'reason':'Imported and current batches may reuse run keys. Namespace imported IDs.',
        'raw_sha256':hashlib.sha256(raw).hexdigest(),'initialized_sha256':hashlib.sha256(data).hexdigest(),
        'entries':len(rows),'amounts_and_usage_unchanged':True,'official_results_modified':False}
    with (output/'budget-ledger-import.json').open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(proof,stream,ensure_ascii=False,indent=2);stream.write('\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();prepare(args.output.resolve())
