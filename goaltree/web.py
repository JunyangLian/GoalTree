"""GoalTree web entry point."""
from pico.web import Workbench, main, make_server

__all__ = ['Workbench', 'make_server', 'main']

if __name__ == '__main__':
    raise SystemExit(main())
