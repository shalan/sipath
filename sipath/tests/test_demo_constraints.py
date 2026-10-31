"""Constraint separation, elaboration and waiver acceptance regression tests."""
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from sipath.design import Design
from sipath import synth, designfile, params
from sipath.tools.openroad import tcl
from sipath.steps.common import design_rule_counts


class DemoConstraints(unittest.TestCase):
    def test_slang_probe_uses_command_registry(self):
        ctx = SimpleNamespace(path=lambda name: name)
        runner = SimpleNamespace(image="test-slang-registry")
        with patch.dict(synth._SLANG, {}, clear=True), patch.object(synth, "_runner", return_value=runner), patch.object(synth.yosys, "command", side_effect=["  read_verilog read RTL\n", "  read_slang    Slang frontend\n"]) as command:
            self.assertEqual(synth.slang_support(ctx), "slang-plugin")
            self.assertEqual(command.call_args_list[0].args[1], ["-p", "help"])
            self.assertEqual(command.call_args_list[1].args[1], ["-p", "plugin -i slang; help"])
        with patch.dict(synth._SLANG, {}, clear=True), patch.object(synth, "_runner", return_value=runner), patch.object(synth.yosys, "command", return_value="No such command: read_slang"):
            self.assertIsNone(synth.slang_support(ctx))

    def test_parameter_elaboration_and_injection_guard(self):
        d=Design('x','top',None,'x.sdc','macro',rtl=['top.sv'],rtl_parameters={'BACKEND':1})
        self.assertIn('-GBACKEND=1', synth._read_rtl(d,False,'slang')[-1])
        self.assertEqual(synth._read_rtl(d,False,'yosys')[-1], 'chparam -set BACKEND 1 top')
        self.assertNotIn('--keep-hierarchy', synth._read_rtl(d, False, 'slang')[-1])
        d.rtl_keep_hierarchy = True
        self.assertIn('--keep-hierarchy', synth._read_rtl(d, False, 'slang')[-1])
        d.rtl_parameters={'BACKEND':'1; shell echo bad'}
        with self.assertRaises(ValueError): synth._read_rtl(d,False,'slang')

    def test_blackbox_interfaces_precede_slang_and_are_inputs(self):
        d = designfile.design_from_dict(dict(name='x', top='top', mode='macro', pdk='sky130A',
             sdc='impl.sdc', rtl=['top.sv'], rtl_blackboxes=['ram.v']), '/tmp')
        self.assertIn('/tmp/ram.v', d.input_files())
        commands = synth._read_rtl(d, False, 'slang-plugin')
        self.assertEqual(commands[0], 'read_verilog -lib -overwrite /tmp/ram.v')
        self.assertIn('--extern-modules', commands[-1])

    def test_signoff_file_is_checked_and_fingerprinted_input(self):
        d=designfile.design_from_dict(dict(name='x',top='top',mode='macro',pdk='sky130A',
             sdc='impl.sdc',signoff_sdc='signoff.sdc',rtl=['top.sv'],rtl_parameters={'BACKEND':1}),'/tmp')
        self.assertIn('/tmp/signoff.sdc',d.input_files())
        self.assertEqual(d.rtl_parameters,{'BACKEND':1})

    def test_library_slew_does_not_remove_cap_fanout_or_boundary(self):
        d=Design('x','top','x.v','x.sdc','macro')
        p=SimpleNamespace(constraints=dict(max_transition=.75,max_capacitance=.2,max_fanout=10,output_load=.02))
        self.assertIn('set_max_transition', '\n'.join(tcl.constraints_setup(p,d)))
        text='\n'.join(tcl.constraints_setup(p,d,library_slew=True))
        self.assertNotIn('set_max_transition',text)
        for command in ['set_max_capacitance','set_max_fanout','set_load']:
            self.assertIn(command,text)

    def classify(self, rows, **kw):
        ctx=SimpleNamespace(session=None,outputs={})
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'drv.rpt';path.write_text(rows)
            with patch('sipath.steps.common.db.inst_master_types', return_value={'u':'CORE','ram':'BLOCK'}):
                return design_rule_counts(ctx,str(path),**kw)

    def test_waiver_boundaries(self):
        for measured,slack,accept in [('1.549','-0.049',True),('1.550','-0.050',True),('1.550000001','-0.050000001',False)]:
            r=self.classify(f'max slew\nu/A 1.500 {measured} {slack} (VIOLATED)\n',slew_waiver_ns=.05)
            self.assertEqual(r['drv_clean'],accept)
            self.assertEqual(r['drv_slew_raw'],1)
            self.assertEqual(r['drv_slew_waived'],int(accept))
        r=self.classify('max slew\nu/Y 1.490 1.545 -0.055 (VIOLATED)\n',slew_waiver_ns=.05)
        self.assertFalse(r['drv_clean'])

    def test_macro_and_cap_not_silently_waived(self):
        text='max slew\nram/CLK 1.5 1.6 -0.1 (VIOLATED)\nmax capacitance\nu/Y 0.04 0.041 -0.001 (VIOLATED)\n'
        r=self.classify(text,include_all=True,slew_waiver_ns=.05)
        self.assertEqual((r['drv_slew'],r['drv_cap'],r['drv_excluded']),(1,1,0))
        self.assertFalse(r['drv_clean'])

    def test_malformed_waiver_evidence_fails(self):
        with self.assertRaises(ValueError):
            self.classify('max slew\nu/A unknown (VIOLATED)\n',slew_waiver_ns=.05)


if __name__=='__main__': unittest.main()
