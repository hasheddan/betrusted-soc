#!/usr/bin/env python3

import sys
sys.path.append("../")    # FIXME
sys.path.append("../../") # FIXME

import lxbuildenv

# This variable defines all the external programs that this module
# relies on.  lxbuildenv reads this variable in order to ensure
# the build will finish without exiting due to missing third-party
# programs.
LX_DEPENDENCIES = ["riscv", "vivado"]

# print('\n'.join(sys.path))  # help with debugging PYTHONPATH issues

from migen import *

from litex.build.generic_platform import *
from litex.build.xilinx import XilinxPlatform

from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import *
from litex.soc.cores.clock import *

from gateware import sram_32
from gateware import spinor

sim_config = {
    # freqs
    "input_clk_freq": 12e6,
#    "sys_clk_freq": 12e6,  # UP5K-side
#    "spi_clk_freq": 24e6,
    "sys_clk_freq": 100e6,  # Artix-side
    "spinor_clk_freq": 100e6,
    "idelay_ref_freq": 200e6,
}


_io = [
    ("clk12", 0, Pins("X")),
    ("rst", 0, Pins("X")),

    ("serial", 0,
     Subsignal("tx", Pins("V6")),
     Subsignal("rx", Pins("V7")),
     IOStandard("LVCMOS18"),
     ),

    # SPI Flash
    ("spiflash_4x", 0,  # clock needs to be accessed through STARTUPE2
     Subsignal("cs_n", Pins("M13")),
     Subsignal("dq", Pins("K17 K18 L14 M15")),
     IOStandard("LVCMOS18")
     ),
    ("spiflash_1x", 0,  # clock needs to be accessed through STARTUPE2
     Subsignal("cs_n", Pins("M13")),
     Subsignal("mosi", Pins("K17")),
     Subsignal("miso", Pins("K18")),
     Subsignal("wp", Pins("L14")),  # provisional
     Subsignal("hold", Pins("M15")),  # provisional
     IOStandard("LVCMOS18")
     ),
    ("spiflash_8x", 0,  # clock needs to be accessed through STARTUPE2
     Subsignal("cs_n", Pins("M13")),
     Subsignal("dq", Pins("K17 K18 L14 M15 L17 L18 M14 N14")),
     Subsignal("dqs", Pins("R14")),
     Subsignal("ecs_n", Pins("L16")),
     Subsignal("sclk", Pins("L13")),
     IOStandard("LVCMOS18")
     ),

    # SRAM
    ("sram", 0,
     Subsignal("adr", Pins(
         "V12 M5 P5 N4  V14 M3 R17 U15",
         "M4  L6 K3 R18 U16 K1 R5  T2",
         "U1  N1 L5 K2  M18 T6"),
               IOStandard("LVCMOS18")),
     Subsignal("ce_n", Pins("V5"), IOStandard("LVCMOS18")),
     Subsignal("oe_n", Pins("U12"), IOStandard("LVCMOS18")),
     Subsignal("we_n", Pins("K4"), IOStandard("LVCMOS18")),
     Subsignal("zz_n", Pins("V17"), IOStandard("LVCMOS18")),
     Subsignal("d", Pins(
         "M2  R4  P2  L4  L1  M1  R1  P1 "
         "U3  V2  V4  U2  N2  T1  K6  J6 "
         "V16 V15 U17 U18 P17 T18 P18 M17 "
         "N3  T4  V13 P15 T14 R15 T3  R7 "), IOStandard("LVCMOS18")),
     Subsignal("dm_n", Pins("V3 R2 T5 T13"), IOStandard("LVCMOS18")),
     ),
]

class Platform(XilinxPlatform):
    def __init__(self):
        XilinxPlatform.__init__(self, "", _io, toolchain="vivado")


class CRG(Module, AutoCSR):
    def __init__(self, platform, core_config):
        # build a simulated PLL. You can add more pll.create_clkout() lines to add more clock frequencies as necessary
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_spinor = ClockDomain()
        self.clock_domains.cd_idelay_ref = ClockDomain()

        self.submodules.pll = pll = S7MMCM()
        self.comb += pll.reset.eq(platform.request("rst"))
        pll.register_clkin(platform.request("clk12"), sim_config["input_clk_freq"])
        pll.create_clkout(self.cd_sys, sim_config["sys_clk_freq"], margin=0)
        pll.create_clkout(self.cd_spinor, sim_config["spinor_clk_freq"], phase=82.5) # hard coded phase, check application code for value
        pll.create_clkout(self.cd_idelay_ref, sim_config["idelay_ref_freq"])

        # Add an IDELAYCTRL primitive for the SpiOpi block
        reset_counter = Signal(5, reset=31)  # 155ns @ 200MHz, min 59.28ns
        ic_reset = Signal(reset=1)
        self.sync.idelay_ref += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.delay_rdy = Signal()
        self.specials += Instance("IDELAYCTRL", i_REFCLK=self.cd_idelay_ref.clk, i_RST=ic_reset, o_RDY=self.delay_rdy)
        self.ready = CSRStatus()
        self.comb += self.ready.status.eq(self.delay_rdy)


class WarmBoot(Module, AutoCSR):
    def __init__(self, parent, reset_vector=0):
        self.ctrl = CSRStorage(size=8)
        self.addr = CSRStorage(size=32, reset=reset_vector)
        self.do_reset = Signal()
        # "Reset Key" is 0xac (0b101011xx)
        self.comb += self.do_reset.eq((self.ctrl.storage & 0xfc) == 0xac)

boot_offset    = 0x0 #0x500000 # enough space to hold 2x FPGA bitstreams before the firmware start

class SimpleSim(SoCCore):
    mem_map = {
        "spiflash": 0x20000000,
        "sram_ext": 0x40000000,
    }
    mem_map.update(SoCCore.mem_map)

    def __init__(self, platform, **kwargs):
        SoCCore.__init__(self, platform, sim_config["sys_clk_freq"],
                         integrated_rom_size=0x8000,
                         integrated_sram_size=0x20000,
                         ident="betrusted.io LiteX Base SoC",
                         cpu_type="vexriscv",
                         **kwargs)

        kwargs["cpu_reset_address"] = self.mem_map["spiflash"]+boot_offset
        self.submodules.reboot = WarmBoot(self, reset_vector=kwargs["cpu_reset_address"])
        self.add_csr("reboot")
        warm_reset = Signal()
        self.comb += warm_reset.eq(self.reboot.do_reset)
        self.cpu.cpu_params.update(i_externalResetVector=self.reboot.addr.storage)

        self.add_constant("SIMULATION", 1)
        self.add_constant("SPIFLASH_SIMULATION", 1)

        # instantiate the clock module
        self.submodules.crg = CRG(platform, sim_config)
        self.add_csr("crg")
        # self.platform.add_period_constraint(self.crg.cd_sys.clk, 1e9/sim_config["sys_clk_freq"])

        self.platform.add_platform_command(
            "create_clock -name clk12 -period 83.3333 [get_nets clk12]")

        # external SRAM to make BIOS build happy
        self.submodules.sram_ext = sram_32.SRAM32(platform.request("sram"), rd_timing=7, wr_timing=6, page_rd_timing=2)
        self.add_csr("sram_ext")
        self.register_mem("sram_ext", self.mem_map["sram_ext"],
                  self.sram_ext.bus, size=0x1000000)

        # spi control -- that's the point of this simulation!
        SPI_FLASH_SIZE=128 * 1024 * 1024
        sclk_instance_name = "SCLK_ODDR"
        iddr_instance_name = "SPI_IDDR"
        miso_instance_name = "MISO_FDRE"
        self.submodules.spinor = spinor.SpiOpi(platform.request("spiflash_8x"),
                                               sclk_name=sclk_instance_name, iddr_name=iddr_instance_name,
                                               miso_name=miso_instance_name, sim=True)
        platform.add_source("../../gateware/spimemio.v") ### NOTE: this actually doesn't help for SIM, but it reminds us to scroll to the bottom of this file and add it to the xvlog imports
        self.register_mem("spiflash", self.mem_map["spiflash"], self.spinor.bus, size=SPI_FLASH_SIZE)
        self.add_csr("spinor")


def generate_top():
    platform = Platform()
    soc = SimpleSim(platform)
    builder = Builder(soc, output_dir="./run", csr_csv="test/csr.csv")
    builder.software_packages = [
    ("libcompiler_rt", os.path.abspath(os.path.join(os.path.dirname(__file__), "../bios/libcompiler_rt"))),
    ("libbase", os.path.abspath(os.path.join(os.path.dirname(__file__), "../bios/libbase"))),
    ("bios", os.path.abspath(os.path.join(os.path.dirname(__file__), "../bios")))
]
    vns = builder.build(run=False)
    soc.do_exit(vns)

    # generate a .init file for the SPINOR memory based on the BIOS we want to boot
    with open("run/software/bios/bios.bin", "rb") as ifile:
        with open("betrusted-soc.init", "w") as ofile:
            binfile = ifile.read()

            for b in binfile:
                ofile.write("{:02x}\n".format(b))

#    platform.build(soc, build_dir="./run", run=False)  # run=False prevents synthesis from happening, but a top.v file gets kicked out

# this generates a test bench wrapper verilog file, needed by the xilinx tools
def generate_top_tb():
    f = open("run/top_tb.v", "w")
    f.write("""
`timescale 1ns/1ps

module top_tb();

reg clk12;
initial clk12 = 1'b1;
always #41.66666 clk12 = ~clk12;

wire sclk;
wire [7:0] sio;
wire dqs;
reg  dqs_delay;  // simulate the clock delay of the FPGA 
wire ecsb;
wire csn;
reg reset;

reg fpga_reset;

initial begin
  reset = 1'b1;
  #11_000;
  reset = 1'b0;
end

initial begin
  fpga_reset = 1'b1;  // fpga reset is extra-long to get past init delays of SPINOR; in reality, this is all handled by the config engine
  #40_000;
  fpga_reset = 1'b0;
end   

MX66UM1G45G rom(
  .SCLK(sclk),
  .CS(csn),
  .SIO(sio),
  .DQS(dqs),
  .ECSB(ecsb),
  .RESET(~reset)
);

always #2.5 dqs_delay = dqs;  // this inserts the equivalent FPGA input clock buffer delay

top dut (
    .spiflash_8x_cs_n(csn),
    .spiflash_8x_dq(sio),
    .spiflash_8x_dqs(dqs_delay),
    .spiflash_8x_ecs_n(ecsb),
    .spiflash_8x_sclk(sclk),

    .clk12(clk12),
    .rst(fpga_reset)
);

endmodule""")
    f.close()


# this ties it all together
def run_sim(gui=False):
    os.system("mkdir -p run")
    os.system("rm -rf run/xsim.dir")
    if sys.platform == "win32":
        call_cmd = "call "
    else:
        call_cmd = ""
    os.system(call_cmd + "cd run && cp gateware/*.init .")
    os.system(call_cmd + "cd run && cp gateware/*.v .")
    os.system(call_cmd + "cd run && xvlog ../../glbl.v")
    os.system(call_cmd + "cd run && xvlog top.v -sv")
    os.system(call_cmd + "cd run && xvlog top_tb.v -sv ")
    #os.system(call_cmd + "cd run && xvlog ../../../deps/litex/litex/soc/cores/cpu/vexriscv/verilog/VexRiscv.v")
    os.system(call_cmd + "cd run && xvlog ../../../gateware/cpu/VexRiscv_BetrustedSoC_Debug.v")
    os.system(call_cmd + "cd run && xvlog ../../../gateware/spimemio.v")
    os.system(call_cmd + "cd run && xvlog ../MX66UM1G45G/MX66UM1G45G.v")
    os.system(call_cmd + "cd run && xelab -debug typical top_tb glbl -s top_tb_sim -L unisims_ver -L unimacro_ver -L SIMPRIM_VER -L secureip -L $xsimdir/xil_defaultlib -timescale 1ns/1ps")
    if gui:
        os.system(call_cmd + "cd run && xsim top_tb_sim -gui")
    else:
        os.system(call_cmd + "cd run && xsim top_tb_sim -runall")


def main():
    import subprocess
    subprocess.Popen(['cp', '../bios/linker_spi.ld', '../bios/linker.ld'])
    generate_top()
    generate_top_tb()
    run_sim(gui=True)


if __name__ == "__main__":
    main()
