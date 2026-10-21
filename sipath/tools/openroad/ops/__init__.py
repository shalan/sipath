"""OpenROAD operations the steps use: each function sends one fixed piece of Tcl
to a session and returns what came back (or that parsed). The steps keep the
stage logic and talk to OpenROAD only through these modules (a unit test
checks that no step module calls a session itself)."""
