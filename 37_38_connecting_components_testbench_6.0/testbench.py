import cocotb
from cocotb.triggers import ClockCycles
from pyuvm import *
import random
from pathlib import Path
import sys
# All testbenches use tinyalu_utils, so store it in a central
# place and add its path to the sys path so we can import it
sys.path.insert(0, str(Path("..").resolve()))
from tinyalu_utils import TinyAluBfm, Ops, alu_prediction  # noqa: E402


# # Testbench components
# ## The testers

class BaseTester(uvm_component):

    def build_phase(self):
        self.pp = uvm_put_port("pp", self)

    async def run_phase(self):
        self.raise_objection()
        self.bfm = TinyAluBfm()
        ops = list(Ops)
        for op in ops:
            aa, bb = self.get_operands()
            cmd_tuple = (aa, bb, op)
            await self.pp.put(cmd_tuple)
        await ClockCycles(signal=cocotb.top.clk, num_cycles=10,
                          rising=False)
        self.drop_objection()

    def get_operands(self):
        raise RuntimeError("You must extend BaseTester and override it.")


# ### RandomTester and MaxTester
class RandomTester(BaseTester):
    def get_operands(self):
        return random.randint(0, 255), random.randint(0, 255)


class MaxTester(BaseTester):
    def get_operands(self):
        return 0xFF, 0xFF


# ## Driver
class Driver(uvm_driver):

    def build_phase(self):
        self.bfm = TinyAluBfm()
        self.gp = uvm_get_port("gp", self)

    async def run_phase(self):
        await self.bfm.reset()
        self.bfm.start_tasks()
        while True:
            aa, bb, op = await self.gp.get()
            await self.bfm.send_op(aa, bb, op)


# ## Monitor
class Monitor(uvm_monitor):
    def __init__(self, name, parent, method_name):
        super().__init__(name, parent)
        self.method_name = method_name

    def build_phase(self):
        self.ap = uvm_analysis_port("ap", self)
        self.bfm = TinyAluBfm()
        self.get_method = getattr(self.bfm, self.method_name)

    async def run_phase(self):
        while True:
            datum = await self.get_method()
            self.ap.write(datum)


# ## Coverage
class Coverage(uvm_analysis_export):
    def start_of_simulation_phase(self):
        self.cvg = set()

    def write(self, cmd):
        _, _, op = cmd
        self.cvg.add(Ops(op))

    def check_phase(self):
        if len(set(Ops) - self.cvg) > 0:
            self.logger.error(
                f"Functional coverage error. Missed: {set(Ops)-self.cvg}")
            assert False
        else:
            self.logger.info("Covered all operations")


# ## Scoreboard
class Scoreboard(uvm_component):

    def build_phase(self):
        self.cmd_mon_fifo = uvm_tlm_analysis_fifo("cmd_mon_fifo", self)
        self.result_mon_fifo = uvm_tlm_analysis_fifo("result_mon_fifo", self)
        self.cmd_gp = uvm_get_port("cmd_gp", self)
        self.result_gp = uvm_get_port("result_gp", self)

    def connect_phase(self):
        self.cmd_gp.connect(self.cmd_mon_fifo.get_export)
        self.result_gp.connect(self.result_mon_fifo.get_export)
        self.cmd_export = self.cmd_mon_fifo.analysis_export
        self.result_export = self.result_mon_fifo.analysis_export

    def check_phase(self):
        passed = True
        while True:
            got_next_cmd, cmd = self.cmd_gp.try_get()
            if not got_next_cmd:
                break
            result_exists, actual = self.result_gp.try_get()
            if not result_exists:
                raise RuntimeError(f"Missing result for command {cmd}")
            (aa, bb, op) = cmd
            prediction = alu_prediction(aa, bb, Ops(op))
            if actual == prediction:
                self.logger.info(
                    f"PASSED: {aa:02x} {Ops(op).name} {bb:02x} = {actual:04x}")
            else:
                passed = False
                self.logger.error(
                    f"FAILED: {aa:02x} {Ops(op).name} {bb:02x} ="
                    f" {actual:04x} - predicted {prediction:04x}")
        assert passed


class AluEnv(uvm_env):

    # ### AluEnv.build_phase()
    def build_phase(self):
        self.tester = BaseTester.create("tester", self)
        self.driver = Driver("driver", self)
        self.cmd_fifo = uvm_tlm_fifo("cmd_fifo", self)
        self.scoreboard = Scoreboard("scoreboard", self)
        self.coverage = Coverage("coverage", self)
        self.cmd_mon = Monitor("cmd_monitor", self, "get_cmd")
        self.result_mon = Monitor("result_monitor", self, "get_result")

    # ### AluEnv.connect_phase()
    def connect_phase(self):
        self.tester.pp.connect(self.cmd_fifo.put_export)
        self.driver.gp.connect(self.cmd_fifo.get_export)

        self.cmd_mon.ap.connect(self.coverage)
        self.cmd_mon.ap.connect(self.scoreboard.cmd_export)

        self.result_mon.ap.connect(self.scoreboard.result_export)


# ## `RandomTest` and `MaxTest`
class RandomTest(uvm_test):
    def build_phase(self):
        uvm_factory().set_type_override_by_type(BaseTester, RandomTester)
        self.env = AluEnv("env", self)


class MaxTest(uvm_test):
    def build_phase(self):
        uvm_factory().set_type_override_by_type(BaseTester, MaxTester)
        self.env = AluEnv("env", self)


@cocotb.test()
async def random_test(_):
    await uvm_root().run_test(RandomTest)


@cocotb.test()
async def max_test(_):
    await uvm_root().run_test(MaxTest)
