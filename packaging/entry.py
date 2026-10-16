"""PyInstaller entry point (the package's __main__ uses relative imports)."""

from usbshare.__main__ import main

raise SystemExit(main())
