"""Render and validate a sourced daily draft; never call a model or fetch data."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.ny_market import validate_payload,report_markdown_sha256
from lib.ny_market_published import render,CONTRACT
from tools.company_news_atomic import atomic_write_json
def build(draft):
    p={**draft,'quality_status':'published','publication_contract_version':CONTRACT}
    p['report_markdown']=render(p)
    p['report_delivery']={'source_field':'report_markdown','sha256':report_markdown_sha256(p['report_markdown'])}
    return validate_payload(p).payload
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('draft',type=Path);ap.add_argument('--output',required=True,type=Path);a=ap.parse_args()
    if a.output.exists():raise ValueError('use a new payload revision')
    p=build(json.loads(a.draft.read_text(encoding='utf-8-sig')));atomic_write_json(a.output,p)
    print(json.dumps({'quality_status':p['quality_status'],'sha256':p['report_delivery']['sha256']}))
if __name__=='__main__':main()
