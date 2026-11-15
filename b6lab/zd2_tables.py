"""Knob display tables read from the DSP code inside a ZD2 effect file.

Every ZD2 embeds a little-endian ELF image (TI C6000) starting at b"\\x7fELF". Its `.const`
section holds the effect descriptor (usually at its start): 0x38-byte records, the first two
for the on/off switch ("OnOff") and the effect itself, then one per knob in knob order:

  +0x00  name, NUL-padded (12 bytes)
  +0x0C  u32 maximum raw value
  +0x10  u32 default raw value
  +0x1C  u32 address of the knob's edit function (.text)
  +0x24  u32 address of its display function ("GetString", .text), 0 for plain numbers

Some display functions print an entry of a fixed-width string table, for example
`_GetString_Mid_F` of AMPG SVT shows `disp_prm_FreqTable8` (32 x 8 bytes: "  32 " .. "6.3K").
The image has no relocations: the code builds the table address with an MVKL/MVKH pair, so
`display_table` decodes the function's 32-bit instructions (skipping the 16-bit compact
words that the fetch packet header marks) for those constants and links the function to the
sized `.const` object they point at. A table is returned only when exactly one such object is
referenced and it holds exactly maximum + 1 entries: printable NUL-padded fixed-width strings,
or u32 pointers to printable NUL-terminated strings in `.const`. Otherwise None.
"""
import struct
from dataclasses import dataclass

RECORD_SIZE = 0x38
FIRST_KNOB_RECORD = 2
SHT_PROGBITS, SHT_SYMTAB = 1, 2


@dataclass
class KnobRecord:
    name: str
    maximum: int
    default: int
    edit: int
    get_string: int


class Elf:
    """The few parts of the ZD2's ELF image this module needs."""

    def __init__(self, zd2):
        start = zd2.find(b"\x7fELF")
        if start < 0:
            raise ValueError("no ELF image in the effect file")
        e = self.data = zd2[start:]
        if e[4] != 1 or e[5] != 1:
            raise ValueError("not a 32-bit little-endian ELF image")
        shoff, = struct.unpack_from("<I", e, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", e, 0x2E)
        self.sections = [struct.unpack_from("<10I", e, shoff + i * shentsize)
                         for i in range(shnum)]
        names = self.sections[shstrndx][4]
        self.section_names = [self._cstr(names + s[0]) for s in self.sections]
        self.symbols = []  # (name, value, size, section index)
        for s in self.sections:
            if s[1] != SHT_SYMTAB:
                continue
            strtab = self.sections[s[6]][4]
            for j in range(s[5] // 16):
                name, value, size, _info, _other, shndx = struct.unpack_from(
                    "<IIIBBH", e, s[4] + j * 16)
                self.symbols.append((self._cstr(strtab + name), value, size, shndx))

    def _cstr(self, off):
        return self.data[off:self.data.index(b"\0", off)].decode("latin-1")

    def section(self, name):
        """(address, bytes) of the PROGBITS section `name`."""
        for header, sname in zip(self.sections, self.section_names):
            if sname == name and header[1] == SHT_PROGBITS:
                return header[3], self.data[header[4]:header[4] + header[5]]
        raise ValueError(f"no {name} section")

    def objects(self, section):
        """{address: (name, size)} of the sized symbols in `section`."""
        out = {}
        for name, value, size, shndx in self.symbols:
            if size and shndx < len(self.sections) and self.section_names[shndx] == section:
                out.setdefault(value, (name, size))
        return out

    def function_end(self, address):
        """Address of the next `.text` symbol after `address` (or the section end)."""
        base, text = self.section(".text")
        ends = [value for name, value, _size, shndx in self.symbols
                if shndx < len(self.sections) and self.section_names[shndx] == ".text"
                and value > address and name and not name.startswith("$")]
        return min(ends, default=base + len(text))


def descriptor(elf):
    """The knob records of the effect descriptor (the one 4-aligned "OnOff" record in `.const`)."""
    _base, const = elf.section(".const")
    starts = [i for i in range(0, len(const) - RECORD_SIZE + 1, 4)
              if const.startswith(b"OnOff\0", i)]
    if len(starts) != 1:
        raise ValueError("no unique effect descriptor")
    records = []
    off = starts[0] + FIRST_KNOB_RECORD * RECORD_SIZE
    while off + RECORD_SIZE <= len(const):
        raw_name = const[off:off + 12].split(b"\0", 1)[0]
        if not raw_name or not all(32 <= c < 127 for c in raw_name):
            break
        maximum, default = struct.unpack_from("<II", const, off + 0x0C)
        edit, = struct.unpack_from("<I", const, off + 0x1C)
        get_string, = struct.unpack_from("<I", const, off + 0x24)
        records.append(KnobRecord(raw_name.decode("ascii"), maximum, default, edit, get_string))
        off += RECORD_SIZE
    return records


def _words(elf, start, end):
    """The 32-bit (non-compact) instruction words in [start, end)."""
    base, text = elf.section(".text")
    packet = (start - base) // 32 * 32
    while packet < end - base and packet + 32 <= len(text):
        words = struct.unpack_from("<8I", text, packet)
        compact = 0
        if words[7] >> 28 == 0b1110:  # fetch packet header: bit 21+i = word i is compact
            compact = (words[7] >> 21) & 0x7F | 0x80
        for i, word in enumerate(words):
            address = base + packet + 4 * i
            if not compact >> i & 1 and start <= address and address + 4 <= end:
                yield word
        packet += 32


def mvk_constants(elf, start, end):
    """{register: (lows, highs)} of the MVK/MVKL and MVKH 16-bit constants in [start, end)."""
    regs = {}
    for word in _words(elf, start, end):
        if word >> 2 & 0xF != 0b1010:  # S-unit MVK family: bits 5..2 = 1010, bit 6 = h
            continue
        reg = (word >> 1 & 1, word >> 23 & 0x1F)  # (side, register)
        cst = word >> 7 & 0xFFFF
        lows, highs = regs.setdefault(reg, (set(), set()))
        (highs if word >> 6 & 1 else lows).add(cst)
    return regs


def referenced_objects(elf, function):
    """The sized `.const` objects whose address `function` builds with MVKL/MVKH."""
    objects = elf.objects(".const")
    found = set()
    for lows, highs in mvk_constants(elf, function, elf.function_end(function)).values():
        for hi in highs:
            for lo in lows:
                address = hi << 16 | lo
                if address in objects:
                    found.add(address)
    return {address: objects[address] for address in found}


def _text(entry):
    """The trimmed printable string in NUL-terminated, NUL-padded `entry`, or None."""
    text = entry.split(b"\0", 1)[0]
    if (len(text) == len(entry) or entry[len(text):].strip(b"\0")
            or not all(32 <= c < 127 for c in text)):
        return None
    return text.decode("ascii").strip() or None


def split_table(raw, count):
    """`raw` as `count` fixed-width printable strings (trimmed), or None.

    Every entry must be NUL-terminated and NUL-padded, so a wrong width cannot cut strings."""
    if count <= 0 or len(raw) % count:
        return None
    width = len(raw) // count
    out = [_text(raw[i * width:(i + 1) * width]) for i in range(count)]
    return None if None in out else out


def pointer_table(raw, count, base, const):
    """`raw` as `count` u32 pointers to NUL-terminated printable strings in `.const`, or None."""
    if count <= 0 or len(raw) != 4 * count:
        return None
    out = []
    for address in struct.unpack(f"<{count}I", raw):
        off = address - base
        end = const.find(b"\0", off) if 0 <= off < len(const) else -1
        text = _text(const[off:end + 1]) if end >= 0 else None
        if text is None:
            return None
        out.append(text)
    return out


def display_table(elf, record):
    """The trimmed display strings of knob `record` (maximum + 1 of them), or None."""
    if not record.get_string:
        return None
    refs = referenced_objects(elf, record.get_string)
    if len(refs) != 1:
        return None
    (address, (_name, size)), = refs.items()
    base, const = elf.section(".const")
    raw = const[address - base:address - base + size]
    if len(raw) != size:
        return None
    count = record.maximum + 1
    return split_table(raw, count) or pointer_table(raw, count, base, const)


def knob_tables(zd2):
    """[(KnobRecord, table or None)] for every knob record of the effect file bytes `zd2`."""
    elf = Elf(zd2)
    return [(record, display_table(elf, record)) for record in descriptor(elf)]


def knob_names(zd2):
    """The names of every knob record of effect file `zd2` in parameter order (trailing Dummy
    and hidden records included), or None without an ELF image or a unique descriptor."""
    try:
        return [record.name for record in descriptor(Elf(zd2))]
    except (ValueError, struct.error, IndexError):
        return None


def knob_labels(zd2, names):
    """Display labels for the knobs `names` (the effect's knobs in order) of effect file `zd2`.

    One entry per name: the table when the descriptor record at that position has exactly that
    name and its display function shows a linked table, else None. Effect files without an ELF
    image or a unique descriptor give all None."""
    try:
        elf = Elf(zd2)
        records = descriptor(elf)
        return [display_table(elf, records[i]) if i < len(records) and records[i].name == name
                else None for i, name in enumerate(names)]
    except (ValueError, struct.error, IndexError):
        return [None] * len(names)
