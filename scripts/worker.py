import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="使用指定数据目录启动后台")
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    os.environ["AUTOBOYA_HOME"] = str(args.home.expanduser().resolve())
    from autoboya.storage import AutoBoyaStore
    from autoboya.logging import configure_logging
    from autoboya.scheduler import AutomationRunner

    store = AutoBoyaStore()
    configure_logging(store, console=False)
    AutomationRunner(store).run_forever()


if __name__ == "__main__":
    main()
