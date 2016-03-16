from litex.gen import *
from litex.gen.genlib.roundrobin import *
from litex.gen.genlib.record import *
from litex.gen.genlib.fsm import FSM, NextState

from litex.soc.interconnect import stream

# TODO: clean up code below
# XXX

def reverse_bytes(signal):
    n = (len(signal)+7)//8
    r = []
    for i in reversed(range(n)):
        r.append(signal[i*8:min((i+1)*8, len(signal))])
    return Cat(iter(r))


class Status(Module):
    def __init__(self, endpoint):
        self.first = first = Signal(reset=1)
        self.eop = eop = Signal()
        self.ongoing = Signal()

        ongoing = Signal()
        self.comb += \
            If(endpoint.stb,
                eop.eq(endpoint.eop & endpoint.ack)
            )
        self.sync += ongoing.eq((endpoint.stb | ongoing) & ~eop)
        self.comb += self.ongoing.eq((endpoint.stb | ongoing) & ~eop)

        self.sync += [
            If(eop,
                first.eq(1)
            ).Elif(endpoint.stb & endpoint.ack,
                first.eq(0)
            )
        ]


class Arbiter(Module):
    def __init__(self, masters, slave):
        if len(masters) == 0:
            pass
        elif len(masters) == 1:
            self.grant = Signal()
            self.comb += masters.pop().connect(slave)
        else:
            self.submodules.rr = RoundRobin(len(masters))
            self.grant = self.rr.grant
            cases = {}
            for i, master in enumerate(masters):
                status = Status(master)
                self.submodules += status
                self.comb += self.rr.request[i].eq(status.ongoing)
                cases[i] = [master.connect(slave)]
            self.comb += Case(self.grant, cases)


class Dispatcher(Module):
    def __init__(self, master, slaves, one_hot=False):
        if len(slaves) == 0:
            self.sel = Signal()
        elif len(slaves) == 1:
            self.comb += master.connect(slaves.pop())
            self.sel = Signal()
        else:
            if one_hot:
                self.sel = Signal(len(slaves))
            else:
                self.sel = Signal(max=len(slaves))

            # # #

            status = Status(master)
            self.submodules += status

            sel = Signal.like(self.sel)
            sel_ongoing = Signal.like(self.sel)
            self.sync += \
                If(status.first,
                    sel_ongoing.eq(self.sel)
                )
            self.comb += \
                If(status.first,
                    sel.eq(self.sel)
                ).Else(
                    sel.eq(sel_ongoing)
                )
            cases = {}
            for i, slave in enumerate(slaves):
                if one_hot:
                    idx = 2**i
                else:
                    idx = i
                cases[idx] = [master.connect(slave)]
            cases["default"] = [master.ack.eq(1)]
            self.comb += Case(sel, cases)


class HeaderField:
    def __init__(self, byte, offset, width):
        self.byte = byte
        self.offset = offset
        self.width = width


class Header:
    def __init__(self, fields, length, swap_field_bytes=True):
        self.fields = fields
        self.length = length
        self.swap_field_bytes = swap_field_bytes

    def get_layout(self):
        layout = []
        for k, v in sorted(self.fields.items()):
            layout.append((k, v.width))
        return layout

    def get_field(self, obj, name, width):
        if "_lsb" in name:
            field = getattr(obj, name.replace("_lsb", ""))[:width]
        elif "_msb" in name:
            field = getattr(obj, name.replace("_msb", ""))[width:2*width]
        else:
            field = getattr(obj, name)
        if len(field) != width:
            raise ValueError("Width mismatch on " + name + " field")
        return field

    def encode(self, obj, signal):
        r = []
        for k, v in sorted(self.fields.items()):
            start = v.byte*8 + v.offset
            end = start + v.width
            field = self.get_field(obj, k, v.width)
            if self.swap_field_bytes:
                field = reverse_bytes(field)
            r.append(signal[start:end].eq(field))
        return r

    def decode(self, signal, obj):
        r = []
        for k, v in sorted(self.fields.items()):
            start = v.byte*8 + v.offset
            end = start + v.width
            field = self.get_field(obj, k, v.width)
            if self.swap_field_bytes:
                r.append(field.eq(reverse_bytes(signal[start:end])))
            else:
                r.append(field.eq(signal[start:end]))
        return r


class Packetizer(Module):
    def __init__(self, sink_description, source_description, header):
        self.sink = sink = stream.Endpoint(sink_description)
        self.source = source = stream.Endpoint(source_description)
        self.header = Signal(header.length*8)

        # # #

        dw = len(self.sink.data)

        header_reg = Signal(header.length*8)
        header_words = (header.length*8)//dw
        load = Signal()
        shift = Signal()
        counter = Signal(max=max(header_words, 2))
        counter_reset = Signal()
        counter_ce = Signal()
        self.sync += \
            If(counter_reset,
                counter.eq(0)
            ).Elif(counter_ce,
                counter.eq(counter + 1)
            )

        self.comb += header.encode(sink, self.header)
        if header_words == 1:
            self.sync += [
                If(load,
                    header_reg.eq(self.header)
                )
            ]
        else:
            self.sync += [
                If(load,
                    header_reg.eq(self.header)
                ).Elif(shift,
                    header_reg.eq(Cat(header_reg[dw:], Signal(dw)))
                )
            ]

        fsm = FSM(reset_state="IDLE")
        self.submodules += fsm

        if header_words == 1:
            idle_next_state = "COPY"
        else:
            idle_next_state = "SEND_HEADER"

        fsm.act("IDLE",
            sink.ack.eq(1),
            counter_reset.eq(1),
            If(sink.stb,
                sink.ack.eq(0),
                source.stb.eq(1),
                source.eop.eq(0),
                source.data.eq(self.header[:dw]),
                If(source.stb & source.ack,
                    load.eq(1),
                    NextState(idle_next_state)
                )
            )
        )
        if header_words != 1:
            fsm.act("SEND_HEADER",
                source.stb.eq(1),
                source.eop.eq(0),
                source.data.eq(header_reg[dw:2*dw]),
                If(source.stb & source.ack,
                    shift.eq(1),
                    counter_ce.eq(1),
                    If(counter == header_words-2,
                        NextState("COPY")
                    )
                )
            )
        fsm.act("COPY",
            source.stb.eq(sink.stb),
            source.eop.eq(sink.eop),
            source.data.eq(sink.data),
            source.error.eq(sink.error),
            If(source.stb & source.ack,
                sink.ack.eq(1),
                If(source.eop,
                    NextState("IDLE")
                )
            )
        )


class Depacketizer(Module):
    def __init__(self, sink_description, source_description, header):
        self.sink = sink = stream.Endpoint(sink_description)
        self.source = source = stream.Endpoint(source_description)
        self.header = Signal(header.length*8)

        # # #

        dw = len(sink.data)

        header_words = (header.length*8)//dw

        shift = Signal()
        counter = Signal(max=max(header_words, 2))
        counter_reset = Signal()
        counter_ce = Signal()
        self.sync += \
            If(counter_reset,
                counter.eq(0)
            ).Elif(counter_ce,
                counter.eq(counter + 1)
            )

        if header_words == 1:
            self.sync += \
                If(shift,
                    self.header.eq(sink.data)
                )
        else:
            self.sync += \
                If(shift,
                    self.header.eq(Cat(self.header[dw:], sink.data))
                )

        fsm = FSM(reset_state="IDLE")
        self.submodules += fsm

        if header_words == 1:
            idle_next_state = "COPY"
        else:
            idle_next_state = "RECEIVE_HEADER"

        fsm.act("IDLE",
            sink.ack.eq(1),
            counter_reset.eq(1),
            If(sink.stb,
                shift.eq(1),
                NextState(idle_next_state)
            )
        )
        if header_words != 1:
            fsm.act("RECEIVE_HEADER",
                sink.ack.eq(1),
                If(sink.stb,
                    counter_ce.eq(1),
                    shift.eq(1),
                    If(counter == header_words-2,
                        NextState("COPY")
                    )
                )
            )
        no_payload = Signal()
        self.sync += \
            If(fsm.before_entering("COPY"),
                no_payload.eq(sink.eop)
            )

        if hasattr(sink, "error"):
            self.comb += source.error.eq(sink.error)
        self.comb += [
            source.eop.eq(sink.eop | no_payload),
            source.data.eq(sink.data),
            header.decode(self.header, source)
        ]
        fsm.act("COPY",
            sink.ack.eq(source.ack),
            source.stb.eq(sink.stb | no_payload),
            If(source.stb & source.ack & source.eop,
                NextState("IDLE")
            )
        )


class Buffer(Module):
    def __init__(self, description, data_depth, cmd_depth=4, almost_full=None):
        self.sink = sink = stream.Endpoint(description)
        self.source = source = stream.Endpoint(description)

        # # #

        sink_status = Status(self.sink)
        source_status = Status(self.source)
        self.submodules += sink_status, source_status

        # store incoming packets
        # cmds
        def cmd_description():
            layout = [("error", 1)]
            return EndpointDescription(layout)
        cmd_fifo = SyncFIFO(cmd_description(), cmd_depth)
        self.submodules += cmd_fifo
        self.comb += cmd_fifo.sink.stb.eq(sink_status.eop)
        if hasattr(sink, "error"):
            self.comb += cmd_fifo.sink.error.eq(sink.error)

        # data
        data_fifo = SyncFIFO(description, data_depth, buffered=True)
        self.submodules += data_fifo
        self.comb += [
            self.sink.connect(data_fifo.sink, leave_out=set(["stb", "ack"])),
            data_fifo.sink.stb.eq(self.sink.stb & cmd_fifo.sink.ack),
            self.sink.ack.eq(data_fifo.sink.ack & cmd_fifo.sink.ack),
        ]

        # output packets
        self.fsm = fsm = FSM(reset_state="IDLE")
        self.submodules += fsm
        fsm.act("IDLE",
            If(cmd_fifo.source.stb,
                NextState("OUTPUT")
            )
        )
        if hasattr(source, "error"):
            source_error = self.source.error
        else:
            source_error = Signal()

        fsm.act("OUTPUT",
            data_fifo.source.connect(self.source, leave_out=set("error")),
            source_error.eq(cmd_fifo.source.error),
            If(source_status.eop,
                cmd_fifo.source.ack.eq(1),
                NextState("IDLE")
            )
        )

        # compute almost full
        if almost_full is not None:
            self.almost_full = Signal()
            self.comb += self.almost_full.eq(data_fifo.level > almost_full)

# XXX
