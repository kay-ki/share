import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui import launch

if __name__ == "__main__":
    app, window = launch()
    sys.exit(app.exec())
