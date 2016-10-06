#!/usr/bin/env python3.5

import argparse

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer
from migen.genlib.cdc import MultiReg
from migen.build.generic_platform import *
from migen.build.xilinx.vivado import XilinxVivadoToolchain
from migen.build.xilinx.ise import XilinxISEToolchain
from migen.fhdl.specials import Keep
from migen.genlib.io import DifferentialInput

from jesd204b.common import (JESD204BTransportSettings,
                            JESD204BPhysicalSettings,
                            JESD204BSettings)
from jesd204b.phy import JESD204BPhyTX
from jesd204b.core import JESD204BCoreTX
from jesd204b.core import JESD204BCoreTXControl

from misoc.interconnect.csr import *
from misoc.interconnect import wishbone
from misoc.cores import gpio
from misoc.cores import spi as spi_csr
from misoc.integration.soc_core import mem_decoder
from misoc.targets.kc705 import MiniSoC, soc_kc705_args, soc_kc705_argdict
from misoc.integration.builder import builder_args, builder_argdict

from artiq.gateware.soc import AMPSoC, build_artiq_soc
from artiq.gateware import rtio, nist_qc1, nist_clock, nist_qc2, phaser
from artiq.gateware.rtio.phy import (ttl_simple, ttl_serdes_7series,
                                     dds, spi, sawg)
from artiq import __version__ as artiq_version


class _RTIOCRG(Module, AutoCSR):
    def __init__(self, platform, rtio_internal_clk):
        self._clock_sel = CSRStorage()
        self._pll_reset = CSRStorage(reset=1)
        self._pll_locked = CSRStatus()
        self.clock_domains.cd_rtio = ClockDomain()
        self.clock_domains.cd_rtiox4 = ClockDomain(reset_less=True)

        # 10 MHz when using 125MHz input
        self.clock_domains.cd_ext_clkout = ClockDomain(reset_less=True)
        ext_clkout = platform.request("user_sma_gpio_p_33")
        self.sync.ext_clkout += ext_clkout.eq(~ext_clkout)


        rtio_external_clk = Signal()
        user_sma_clock = platform.request("user_sma_clock")
        platform.add_period_constraint(user_sma_clock.p, 8.0)
        self.specials += Instance("IBUFDS",
                                  i_I=user_sma_clock.p, i_IB=user_sma_clock.n,
                                  o_O=rtio_external_clk)

        pll_locked = Signal()
        rtio_clk = Signal()
        rtiox4_clk = Signal()
        ext_clkout_clk = Signal()
        self.specials += [
            Instance("PLLE2_ADV",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=pll_locked,

                     p_REF_JITTER1=0.01,
                     p_CLKIN1_PERIOD=8.0, p_CLKIN2_PERIOD=8.0,
                     i_CLKIN1=rtio_internal_clk, i_CLKIN2=rtio_external_clk,
                     # Warning: CLKINSEL=0 means CLKIN2 is selected
                     i_CLKINSEL=~self._clock_sel.storage,

                     # VCO @ 1GHz when using 125MHz input
                     p_CLKFBOUT_MULT=8, p_DIVCLK_DIVIDE=1,
                     i_CLKFBIN=self.cd_rtio.clk,
                     i_RST=self._pll_reset.storage,

                     o_CLKFBOUT=rtio_clk,

                     p_CLKOUT0_DIVIDE=2, p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=rtiox4_clk,

                     p_CLKOUT1_DIVIDE=50, p_CLKOUT1_PHASE=0.0,
                     o_CLKOUT1=ext_clkout_clk),
            Instance("BUFG", i_I=rtio_clk, o_O=self.cd_rtio.clk),
            Instance("BUFG", i_I=rtiox4_clk, o_O=self.cd_rtiox4.clk),
            Instance("BUFG", i_I=ext_clkout_clk, o_O=self.cd_ext_clkout.clk),

            AsyncResetSynchronizer(self.cd_rtio, ~pll_locked),
            MultiReg(pll_locked, self._pll_locked.status)
        ]


# The default user SMA voltage on KC705 is 2.5V, and the Migen platform
# follows this default. But since the SMAs are on the same bank as the DDS,
# which is set to 3.3V by reprogramming the KC705 power ICs, we need to
# redefine them here. 
_sma33_io = [
    ("user_sma_gpio_p_33", 0, Pins("Y23"), IOStandard("LVCMOS33")),
    ("user_sma_gpio_n_33", 0, Pins("Y24"), IOStandard("LVCMOS33")),
]


_ams101_dac = [
    ("ams101_dac", 0,
        Subsignal("ldac", Pins("XADC:GPIO0")),
        Subsignal("clk", Pins("XADC:GPIO1")),
        Subsignal("mosi", Pins("XADC:GPIO2")),
        Subsignal("cs_n", Pins("XADC:GPIO3")),
        IOStandard("LVTTL")
     )
]


class _NIST_Ions(MiniSoC, AMPSoC):
    mem_map = {
        "timer_kernel":  0x10000000, # (shadow @0x90000000)
        "rtio":          0x20000000, # (shadow @0xa0000000)
        "i2c":           0x30000000, # (shadow @0xb0000000)
        "mailbox":       0x70000000  # (shadow @0xf0000000)
    }
    mem_map.update(MiniSoC.mem_map)

    def __init__(self, cpu_type="or1k", **kwargs):
        MiniSoC.__init__(self,
                         cpu_type=cpu_type,
                         sdram_controller_type="minicon",
                         l2_size=128*1024,
                         with_timer=False,
                         ident=artiq_version,
                         **kwargs)
        AMPSoC.__init__(self)
        if isinstance(self.platform.toolchain, XilinxVivadoToolchain):
            self.platform.toolchain.bitstream_commands.extend([
                "set_property BITSTREAM.GENERAL.COMPRESS True [current_design]",
            ])
        if isinstance(self.platform.toolchain, XilinxISEToolchain):
            self.platform.toolchain.bitgen_opt += " -g compress"

        self.submodules.leds = gpio.GPIOOut(Cat(
            self.platform.request("user_led", 0),
            self.platform.request("user_led", 1)))
        self.csr_devices.append("leds")

        self.platform.add_extension(_sma33_io)
        self.platform.add_extension(_ams101_dac)

        i2c = self.platform.request("i2c")
        self.submodules.i2c = gpio.GPIOTristate([i2c.scl, i2c.sda])
        self.register_kernel_cpu_csrdevice("i2c")
        self.config["I2C_BUS_COUNT"] = 1

    def add_rtio(self, rtio_channels, crg=_RTIOCRG):
        self.submodules.rtio_crg = crg(self.platform, self.crg.cd_sys.clk)
        self.csr_devices.append("rtio_crg")
        self.submodules.rtio = rtio.RTIO(rtio_channels)
        self.register_kernel_cpu_csrdevice("rtio")
        self.config["RTIO_FINE_TS_WIDTH"] = self.rtio.fine_ts_width
        self.submodules.rtio_moninj = rtio.MonInj(rtio_channels)
        self.csr_devices.append("rtio_moninj")

        self.specials += [
            Keep(self.rtio.cd_rsys.clk),
            Keep(self.rtio_crg.cd_rtio.clk),
            Keep(self.ethphy.crg.cd_eth_rx.clk),
            Keep(self.ethphy.crg.cd_eth_tx.clk),
        ]

        self.platform.add_period_constraint(self.rtio.cd_rsys.clk, 8.)
        self.platform.add_period_constraint(self.rtio_crg.cd_rtio.clk, 8.)
        self.platform.add_period_constraint(self.ethphy.crg.cd_eth_rx.clk, 8.)
        self.platform.add_period_constraint(self.ethphy.crg.cd_eth_tx.clk, 8.)
        self.platform.add_false_path_constraints(
            self.rtio.cd_rsys.clk,
            self.rtio_crg.cd_rtio.clk,
            self.ethphy.crg.cd_eth_rx.clk,
            self.ethphy.crg.cd_eth_tx.clk)

        self.submodules.rtio_analyzer = rtio.Analyzer(self.rtio,
            self.get_native_sdram_if())
        self.csr_devices.append("rtio_analyzer")


class NIST_QC1(_NIST_Ions):
    """
    NIST QC1 hardware, as used in the Penning lab, with FMC to SCSI cables
    adapter.
    """
    def __init__(self, cpu_type="or1k", **kwargs):
        _NIST_Ions.__init__(self, cpu_type, **kwargs)

        platform = self.platform
        platform.add_extension(nist_qc1.fmc_adapter_io)

        self.comb += [
            platform.request("ttl_l_tx_en").eq(1),
            platform.request("ttl_h_tx_en").eq(1)
        ]

        rtio_channels = []
        for i in range(2):
            phy = ttl_serdes_7series.Inout_8X(platform.request("pmt", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))
        for i in range(15):
            phy = ttl_serdes_7series.Output_8X(platform.request("ttl", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy))

        phy = ttl_serdes_7series.Inout_8X(platform.request("user_sma_gpio_n_33"))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))
        phy = ttl_simple.Output(platform.request("user_led", 2))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))
        self.config["RTIO_REGULAR_TTL_COUNT"] = len(rtio_channels)

        phy = ttl_simple.ClockGen(platform.request("ttl", 15))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))

        self.config["RTIO_FIRST_DDS_CHANNEL"] = len(rtio_channels)
        self.config["RTIO_DDS_COUNT"] = 1
        self.config["DDS_CHANNELS_PER_BUS"] = 8
        self.config["DDS_AD9858"] = True
        phy = dds.AD9858(platform.request("dds"), 8)
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy,
                                                   ofifo_depth=512,
                                                   ififo_depth=4))

        self.config["RTIO_LOG_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.LogChannel())

        self.add_rtio(rtio_channels)
        assert self.rtio.fine_ts_width <= 3
        self.config["DDS_RTIO_CLK_RATIO"] = 8 >> self.rtio.fine_ts_width


class NIST_CLOCK(_NIST_Ions):
    """
    NIST clock hardware, with old backplane and 11 DDS channels
    """
    def __init__(self, cpu_type="or1k", **kwargs):
        _NIST_Ions.__init__(self, cpu_type, **kwargs)

        platform = self.platform
        platform.add_extension(nist_clock.fmc_adapter_io)

        rtio_channels = []
        for i in range(16):
            if i % 4 == 3:
                phy = ttl_serdes_7series.Inout_8X(platform.request("ttl", i))
                self.submodules += phy
                rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))
            else:
                phy = ttl_serdes_7series.Output_8X(platform.request("ttl", i))
                self.submodules += phy
                rtio_channels.append(rtio.Channel.from_phy(phy))

        for i in range(2):
            phy = ttl_serdes_7series.Inout_8X(platform.request("pmt", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))

        phy = ttl_serdes_7series.Inout_8X(platform.request("user_sma_gpio_n_33"))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))

        phy = ttl_simple.Output(platform.request("user_led", 2))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))

        ams101_dac = self.platform.request("ams101_dac", 0)
        phy = ttl_simple.Output(ams101_dac.ldac)
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))
        self.config["RTIO_REGULAR_TTL_COUNT"] = len(rtio_channels)

        phy = ttl_simple.ClockGen(platform.request("la32_p"))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))

        phy = spi.SPIMaster(ams101_dac)
        self.submodules += phy
        self.config["RTIO_FIRST_SPI_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.Channel.from_phy(
            phy, ofifo_depth=4, ififo_depth=4))

        for i in range(3):
            phy = spi.SPIMaster(self.platform.request("spi", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(
                phy, ofifo_depth=128, ififo_depth=128))

        self.config["RTIO_FIRST_DDS_CHANNEL"] = len(rtio_channels)
        self.config["RTIO_DDS_COUNT"] = 1
        self.config["DDS_CHANNELS_PER_BUS"] = 11
        self.config["DDS_AD9914"] = True
        self.config["DDS_ONEHOT_SEL"] = True
        phy = dds.AD9914(platform.request("dds"), 11, onehot=True)
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy,
                                                   ofifo_depth=512,
                                                   ififo_depth=4))

        self.config["RTIO_LOG_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.LogChannel())

        self.add_rtio(rtio_channels)
        assert self.rtio.fine_ts_width <= 3
        self.config["DDS_RTIO_CLK_RATIO"] = 24 >> self.rtio.fine_ts_width


class NIST_QC2(_NIST_Ions):
    """
    NIST QC2 hardware, as used in Quantum I and Quantum II, with new backplane
    and 24 DDS channels.  Two backplanes are used.  
    """
    def __init__(self, cpu_type="or1k", **kwargs):
        _NIST_Ions.__init__(self, cpu_type, **kwargs)

        platform = self.platform
        platform.add_extension(nist_qc2.fmc_adapter_io)

        rtio_channels = []
        clock_generators = []

        # All TTL channels are In+Out capable
        for i in range(40):
            phy = ttl_serdes_7series.Inout_8X(
                platform.request("ttl", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))
        
        # CLK0, CLK1 are for clock generators, on backplane SMP connectors
        for i in range(2):        
            phy = ttl_simple.ClockGen(
                platform.request("clkout", i))
            self.submodules += phy
            clock_generators.append(rtio.Channel.from_phy(phy)) 

        # user SMA on KC705 board
        phy = ttl_serdes_7series.Inout_8X(platform.request("user_sma_gpio_n_33"))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=512))
        
        phy = ttl_simple.Output(platform.request("user_led", 2))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))

        # AMS101 DAC on KC705 XADC header - optional
        ams101_dac = self.platform.request("ams101_dac", 0)
        phy = ttl_simple.Output(ams101_dac.ldac)
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))
        self.config["RTIO_REGULAR_TTL_COUNT"] = len(rtio_channels)

        # add clock generators after RTIO_REGULAR_TTL_COUNT
        rtio_channels += clock_generators

        phy = spi.SPIMaster(ams101_dac)
        self.submodules += phy
        self.config["RTIO_FIRST_SPI_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.Channel.from_phy(
            phy, ofifo_depth=4, ififo_depth=4))

        for i in range(4):
            phy = spi.SPIMaster(self.platform.request("spi", i))
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(
                phy, ofifo_depth=128, ififo_depth=128))

        self.config["RTIO_FIRST_DDS_CHANNEL"] = len(rtio_channels)
        self.config["RTIO_DDS_COUNT"] = 2
        self.config["DDS_CHANNELS_PER_BUS"] = 12
        self.config["DDS_AD9914"] = True
        self.config["DDS_ONEHOT_SEL"] = True
        for backplane_offset in range(2):
            phy = dds.AD9914(
                platform.request("dds", backplane_offset), 12, onehot=True)
            self.submodules += phy
            rtio_channels.append(rtio.Channel.from_phy(phy,
                                                       ofifo_depth=512,
                                                       ififo_depth=4))

        self.config["RTIO_LOG_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.LogChannel())

        self.add_rtio(rtio_channels)
        assert self.rtio.fine_ts_width <= 3
        self.config["DDS_RTIO_CLK_RATIO"] = 24 >> self.rtio.fine_ts_width


class _PhaserCRG(Module, AutoCSR):
    def __init__(self, platform, rtio_internal_clk):
        rtio_internal_clk = ClockSignal("sys4x")

        self._clock_sel = CSRStorage()
        self._pll_reset = CSRStorage(reset=1)
        self._pll_locked = CSRStatus()
        self.clock_domains.cd_rtio = ClockDomain()
        self.clock_domains.cd_rtiox4 = ClockDomain(reset_less=True)

        refclk_pads = platform.request("ad9154_refclk")
        platform.add_period_constraint(refclk_pads.p, 2.)
        self.refclk = Signal()
        self.clock_domains.cd_refclk = ClockDomain()
        self.specials += [
            Instance("IBUFDS_GTE2", i_CEB=0,
                     i_I=refclk_pads.p, i_IB=refclk_pads.n, o_O=self.refclk),
            Instance("BUFG", i_I=self.refclk, o_O=self.cd_refclk.clk),
        ]

        pll_locked = Signal()
        rtio_clk = Signal()
        rtiox4_clk = Signal()
        self.specials += [
            Instance("PLLE2_ADV",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=pll_locked,

                     p_REF_JITTER1=0.01, p_REF_JITTER2=0.01,
                     p_CLKIN1_PERIOD=2.0, p_CLKIN2_PERIOD=2.0,
                     i_CLKIN1=rtio_internal_clk, i_CLKIN2=self.cd_refclk.clk,
                     # Warning: CLKINSEL=0 means CLKIN2 is selected
                     i_CLKINSEL=~self._clock_sel.storage,

                     # VCO @ 1GHz when using 500MHz input
                     p_CLKFBOUT_MULT=8, p_DIVCLK_DIVIDE=4,
                     i_CLKFBIN=self.cd_rtio.clk,
                     i_RST=self._pll_reset.storage,

                     o_CLKFBOUT=rtio_clk,

                     p_CLKOUT0_DIVIDE=2, p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=rtiox4_clk,
                     ),
            Instance("BUFG", i_I=rtio_clk, o_O=self.cd_rtio.clk),
            Instance("BUFG", i_I=rtiox4_clk, o_O=self.cd_rtiox4.clk),

            AsyncResetSynchronizer(self.cd_rtio, ~pll_locked),
            MultiReg(pll_locked, self._pll_locked.status)
        ]


class Phaser(_NIST_Ions):
    mem_map = {
        "ad9154_spi":   0x50000000,
        "jesd_control": 0x40000000,
    }
    mem_map.update(_NIST_Ions.mem_map)

    def __init__(self, cpu_type="or1k", **kwargs):
        _NIST_Ions.__init__(self, cpu_type, **kwargs)

        platform = self.platform
        platform.add_extension(phaser.fmc_adapter_io)

        sysref_pads = platform.request("ad9154_sysref")

        rtio_channels = []

        phy = ttl_serdes_7series.Inout_8X(
            platform.request("user_sma_gpio_n_33"))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=128))

        phy = ttl_simple.Output(platform.request("user_led", 2))
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy))

        phy = ttl_serdes_7series.Input_8X(sysref_pads.p, sysref_pads.n)
        self.submodules += phy
        rtio_channels.append(rtio.Channel.from_phy(phy, ififo_depth=32,
                                                   ofifo_depth=2))

        self.config["RTIO_REGULAR_TTL_COUNT"] = len(rtio_channels)

        ad9154_spi = self.platform.request("ad9154_spi")
        self.submodules.ad9154_spi = spi_csr.SPIMaster(ad9154_spi)
        self.register_kernel_cpu_csrdevice("ad9154_spi")
        self.config["AD9154_DAC_CS"] = 1 << 0
        self.config["AD9154_CLK_CS"] = 1 << 1
        self.comb += [
            ad9154_spi.en.eq(1),
            self.platform.request("ad9154_txen", 0).eq(1),
            self.platform.request("ad9154_txen", 1).eq(1),
        ]

        self.config["RTIO_FIRST_SAWG_CHANNEL"] = len(rtio_channels)
        sawgs = [sawg.Channel(width=16, parallelism=4) for i in range(4)]
        self.submodules += sawgs

        rtio_channels.extend(rtio.Channel.from_phy(phy)
                             for sawg in sawgs
                             for phy in sawg.phys)

        self.config["RTIO_LOG_CHANNEL"] = len(rtio_channels)
        rtio_channels.append(rtio.LogChannel())
        self.add_rtio(rtio_channels, _PhaserCRG)

        # jesd_sysref = Signal()
        # self.specials += DifferentialInput(
        #     sysref_pads.p, sysref_pads.n, jesd_sysref)
        sync_pads = platform.request("ad9154_sync")
        jesd_sync = Signal()
        self.specials += DifferentialInput(
            sync_pads.p, sync_pads.n, jesd_sync)

        ps = JESD204BPhysicalSettings(l=4, m=4, n=16, np=16, sc=250*1e6)
        ts = JESD204BTransportSettings(f=2, s=1, k=16, cs=1)
        jesd_settings = JESD204BSettings(ps, ts, did=0x5a, bid=0x5)
        jesd_linerate = 5e9
        jesd_refclk_freq = 500e6
        rtio_freq = 125*1000*1000
        jesd_phys = [JESD204BPhyTX(
            self.rtio_crg.refclk, jesd_refclk_freq,
            platform.request("ad9154_jesd", i),
            jesd_linerate, rtio_freq, i) for i in range(4)]
        self.submodules += jesd_phys
        for jesd_phy in jesd_phys:
            platform.add_period_constraint(
                jesd_phy.gtx.cd_tx.clk,
                40/jesd_linerate*1e9)
            self.platform.add_false_path_constraints(
                self.rtio_crg.cd_rtio.clk,
                jesd_phy.gtx.cd_tx.clk)
        self.submodules.jesd_core = JESD204BCoreTX(
            jesd_phys, jesd_settings, converter_data_width=32)
        self.comb += self.jesd_core.start.eq(jesd_sync)
        self.submodules.jesd_control = JESD204BCoreTXControl(self.jesd_core)
        self.register_kernel_cpu_csrdevice("jesd_control")
        for i, ch in enumerate(sawgs):
            conv = getattr(self.jesd_core.transport.sink,
                           "converter{}".format(i))
            # while at 5 GBps, take every second sample... FIXME
            self.comb += conv.eq(Cat(ch.o[::2]))


def main():
    parser = argparse.ArgumentParser(
        description="ARTIQ core device builder / KC705 "
                    "+ NIST Ions QC1/CLOCK/QC2 hardware adapters")
    builder_args(parser)
    soc_kc705_args(parser)
    parser.add_argument("-H", "--hw-adapter", default="nist_clock",
                        help="hardware adapter type: "
                             "nist_qc1/nist_clock/nist_qc2/phaser "
                             "(default: %(default)s)")
    args = parser.parse_args()

    hw_adapter = args.hw_adapter.lower()
    if hw_adapter == "nist_qc1":
        cls = NIST_QC1
    elif hw_adapter == "nist_clock":
        cls = NIST_CLOCK
    elif hw_adapter == "nist_qc2":
        cls = NIST_QC2
    elif hw_adapter == "phaser":
        cls = Phaser
    else:
        raise SystemExit("Invalid hardware adapter string (-H/--hw-adapter)")

    soc = cls(**soc_kc705_argdict(args))
    build_artiq_soc(soc, builder_argdict(args))


if __name__ == "__main__":
    main()
