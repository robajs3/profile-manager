import sys

if "--worker" in sys.argv:                      # tryb procesu przeglądarki (bez Qt)
    from pm import worker
    worker.run(sys.argv[sys.argv.index("--worker") + 1])
    sys.exit(0)

from pm.gui import run

if __name__ == "__main__":
    run()
