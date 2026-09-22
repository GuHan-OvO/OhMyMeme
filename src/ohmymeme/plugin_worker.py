"""`python -m ohmymeme.plugin_worker` 入口。"""

import sys


def main(argv=None):
    from ohmymeme.core.plugins.runtime.worker import main as worker_main

    return worker_main(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
