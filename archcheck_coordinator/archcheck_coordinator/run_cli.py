"""Chạy phân tích từ dòng lệnh:  python run_cli.py sample/shop sample/plan.puml"""
import argparse
import json

from archcheck.coordinator import CoordinatorService
from archcheck.storage import Storage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project_path')
    ap.add_argument('plan_path')
    ap.add_argument('--db', default='sqlite:///archcheck.db')
    ap.add_argument('--json', action='store_true', help='in toàn bộ báo cáo JSON')
    args = ap.parse_args()

    state = CoordinatorService(storage=Storage(args.db)).analyze(args.project_path, args.plan_path)
    rep = state['report']
    if args.json:
        rep = {k: v for k, v in rep.items() if k != 'ca_graph'}
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return
    print(f'[{rep["status"]}] {rep["verdict"]}')
    for w in rep['warnings']:
        print('  ! ' + w)
    for v in rep['violations']:
        rec = v['recommendation'] or {}
        flag = ' (đã ẩn theo phản hồi)' if rec.get('suppressed') else ''
        print(f'  - [{v["severity"]}] {v["type"]}: {v["message"]} (id={v["id"]}){flag}')
    print('\nTrace:')
    print(*rep['trace'], sep='\n')


if __name__ == '__main__':
    main()
