"""``python -m teamcraft.api`` runs the local API server (see serve.py)."""

from .serve import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
