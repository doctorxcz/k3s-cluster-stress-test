Technical logs for debugging (hidden folder, you normally do not need it).

debug/   one file per run of the tool: <date>_<time>_<pid>.log
         contains all called kubectl commands (with the return code and
         time), measurements, decisions and any errors with a traceback.
running/ registry of tests started in the background (one .json per running test,
         removed when it finishes)
tests/   one subdirectory per pytest run: pytest.log (results and
         errors), tool-runs/ (debug logs of the tool started from the tests).

Nothing is deleted automatically - the history stays complete.
Test results for the user are in the visible folder ../logs/.
