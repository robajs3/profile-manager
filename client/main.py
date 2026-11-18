import sys

try:   # użyj magazynu certyfikatów systemu (antywirus/proxy z podmianą certyfikatów, firmowe CA)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

if "--worker" in sys.argv:                      # tryb procesu przeglądarki (bez Qt)
    import traceback
    from pm import worker
    try:
        worker.run(sys.argv[sys.argv.index("--worker") + 1])
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)

from pm.gui import run

if __name__ == "__main__":
    run()
