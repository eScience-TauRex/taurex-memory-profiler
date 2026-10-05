"""TauREx memory profiler — measure and plot where the memory of a run goes.

Two commands, one per side of a run:

* ``taurex-mem-run``  (module :mod:`taurex_memory_profiler.runner`)
  runs a command with one :mod:`taurex_memory_profiler.monitor` per node.
* ``taurex-mem-plot`` (module :mod:`taurex_memory_profiler.plot`)
  turns the sampled CSVs into figures, a text summary and a report.

``python -m taurex_memory_profiler`` is the plotter; the sampler and the runner
are also runnable as modules, which is how the runner starts the per-node
samplers.
"""

__version__ = "0.2.0"
