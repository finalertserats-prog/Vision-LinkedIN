"""CLI package — console entry points for the cron-driven processes (§10.2).

Contains thin ``main()`` stubs for ``vision-daily``, ``vision-publisher`` and
``vision-token``. The stubs configure logging and print an intent line so the
entry points are wired and importable now; the real pipeline logic lands in the
respective phases.
"""
