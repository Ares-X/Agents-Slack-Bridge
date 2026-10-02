"""Print undelivered inbox messages as JSON lines. 只读不标记。

每条记录含 msg_id（"<channel>:<ts>"，入队去重与确认的统一身份）。
显示名解析请用 resolve.py（热路径不做慢查询）。
"""
import json

import inbox_store


def main():
    for r in inbox_store.read_undelivered():
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
