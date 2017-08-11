
from __future__ import print_function

import sys
from Dataset import DatasetSeq
from CachedDataset2 import CachedDataset2
import gzip
import xml.etree.ElementTree as etree
from Util import parse_orthography, parse_orthography_into_symbols, load_json, BackendEngine, unicode
from Log import log
import numpy
import time
from random import Random


class LmDataset(CachedDataset2):

  def __init__(self,
               corpus_file,
               orth_symbols_file=None,
               orth_symbols_map_file=None,
               orth_replace_map_file=None,
               word_based=False,
               seq_end_symbol="[END]",
               unknown_symbol="[UNKNOWN]",
               parse_orth_opts=None,
               phone_info=None,
               add_random_phone_seqs=0,
               partition_epoch=1,
               auto_replace_unknown_symbol=False,
               log_auto_replace_unknown_symbols=10,
               log_skipped_seqs=10,
               error_on_invalid_seq=True,
               add_delayed_seq_data=False,
               delayed_seq_data_start_symbol="[START]",
               **kwargs):
    """
    :param str|()->str corpus_file: Bliss XML or line-based txt. optionally can be gzip.
    :param dict|None phone_info: if you want to get phone seqs, dict with lexicon_file etc. see PhoneSeqGenerator
    :param str|()->str|None orth_symbols_file: list of orthography symbols, if you want to get orth symbol seqs
    :param str|()->str|None orth_symbols_map_file: list of orth symbols, each line: "symbol index"
    :param str|()->str|None orth_replace_map_file: JSON file with replacement dict for orth symbols
    :param bool word_based: whether to parse single words, or otherwise will be char-based
    :param str|None seq_end_symbol: what to add at the end, if given.
      will be set as postfix=[seq_end_symbol] or postfix=[] for parse_orth_opts.
    :param dict[str]|None parse_orth_opts: kwargs for parse_orthography()
    :param int add_random_phone_seqs: will add random seqs with the same len as the real seq as additional data
    :param bool|int log_auto_replace_unknown_symbols: write about auto-replacements with unknown symbol.
      if this is an int, it will only log the first N replacements, and then keep quiet.
    :param bool|int log_skipped_seqs: write about skipped seqs to logging, due to missing lexicon entry or so.
      if this is an int, it will only log the first N entries, and then keep quiet.
    :param bool error_on_invalid_seq: if there is a seq we would have to skip, error
    :param bool add_delayed_seq_data: will add another data-key "delayed" which will have the sequence
      delayed_seq_data_start_symbol + original_sequence[:-1]
    :param str delayed_seq_data_start_symbol: used for add_delayed_seq_data
    :param int partition_epoch: whether to partition the epochs into multiple parts. like epoch_split
    """
    super(LmDataset, self).__init__(**kwargs)

    if callable(corpus_file):
      corpus_file = corpus_file()
    if callable(orth_symbols_file):
      orth_symbols_file = orth_symbols_file()
    if callable(orth_symbols_map_file):
      orth_symbols_map_file = orth_symbols_map_file()
    if callable(orth_replace_map_file):
      orth_replace_map_file = orth_replace_map_file()

    print("LmDataset, loading file", corpus_file, file=log.v4)

    self.word_based = word_based
    self.seq_end_symbol = seq_end_symbol
    self.unknown_symbol = unknown_symbol
    self.parse_orth_opts = parse_orth_opts or {}
    self.parse_orth_opts.setdefault("word_based", self.word_based)
    self.parse_orth_opts.setdefault("postfix", [self.seq_end_symbol] if self.seq_end_symbol is not None else [])

    if orth_symbols_file:
      assert not phone_info
      assert not orth_symbols_map_file
      orth_symbols = open(orth_symbols_file).read().splitlines()
      self.orth_symbols_map = {sym: i for (i, sym) in enumerate(orth_symbols)}
      self.orth_symbols = orth_symbols
      self.labels["data"] = orth_symbols
      self.seq_gen = None
    elif orth_symbols_map_file:
      assert not phone_info
      orth_symbols_imap_list = [(int(b), a)
                                for (a, b) in [l.split(None, 1)
                                               for l in open(orth_symbols_map_file).read().splitlines()]]
      orth_symbols_imap_list.sort()
      assert orth_symbols_imap_list[0][0] == 0
      assert orth_symbols_imap_list[-1][0] == len(orth_symbols_imap_list) - 1
      self.orth_symbols_map = {sym: i for (i, sym) in orth_symbols_imap_list}
      self.orth_symbols = [sym for (i, sym) in orth_symbols_imap_list]
      self.labels["data"] = self.orth_symbols
      self.seq_gen = None
    else:
      assert not orth_symbols_file
      assert isinstance(phone_info, dict)
      self.seq_gen = PhoneSeqGenerator(**phone_info)
      self.orth_symbols = None
      self.labels["data"] = self.seq_gen.get_class_labels()
    if orth_replace_map_file:
      orth_replace_map = load_json(filename=orth_replace_map_file)
      assert isinstance(orth_replace_map, dict)
      self.orth_replace_map = {key: parse_orthography_into_symbols(v, word_based=self.word_based)
                               for (key, v) in orth_replace_map.items()}
      if self.orth_replace_map:
        if len(self.orth_replace_map) <= 5:
          print("  orth_replace_map: %r" % self.orth_replace_map, file=log.v5)
        else:
          print("  orth_replace_map: %i entries" % len(self.orth_replace_map), file=log.v5)
    else:
      self.orth_replace_map = {}

    num_labels = len(self.labels["data"])
    use_uint_types = False
    if BackendEngine.is_tensorflow_selected():
      use_uint_types = True
    if num_labels <= 2 ** 7:
      self.dtype = "int8"
    elif num_labels <= 2 ** 8 and use_uint_types:
      self.dtype = "uint8"
    elif num_labels <= 2 ** 31:
      self.dtype = "int32"
    elif num_labels <= 2 ** 32 and use_uint_types:
      self.dtype = "uint32"
    elif num_labels <= 2 ** 61:
      self.dtype = "int64"
    elif num_labels <= 2 ** 62 and use_uint_types:
      self.dtype = "uint64"
    else:
      raise Exception("cannot handle so much labels: %i" % num_labels)
    self.num_outputs = {"data": [len(self.labels["data"]), 1]}
    self.num_inputs = self.num_outputs["data"][0]
    self.seq_order = None
    self.auto_replace_unknown_symbol = auto_replace_unknown_symbol
    self.log_auto_replace_unknown_symbols = log_auto_replace_unknown_symbols
    self.log_skipped_seqs = log_skipped_seqs
    self.error_on_invalid_seq = error_on_invalid_seq
    self.partition_epoch = partition_epoch
    self.add_random_phone_seqs = add_random_phone_seqs
    for i in range(add_random_phone_seqs):
      self.num_outputs["random%i" % i] = self.num_outputs["data"]
    self.add_delayed_seq_data = add_delayed_seq_data
    self.delayed_seq_data_start_symbol = delayed_seq_data_start_symbol
    if add_delayed_seq_data:
      self.num_outputs["delayed"] = self.num_outputs["data"]

    if _is_bliss(corpus_file):
      iter_f = _iter_bliss
    else:
      iter_f = _iter_txt
    self.orths = []
    iter_f(corpus_file, self.orths.append)
    # It's only estimated because we might filter some out or so.
    self._estimated_num_seqs = len(self.orths) // self.partition_epoch
    print("  done, loaded %i sequences" % len(self.orths), file=log.v4)

  def get_target_list(self):
    return sorted([k for k in self.num_outputs.keys() if k != "data"])

  def get_data_dtype(self, key):
    return self.dtype

  def init_seq_order(self, epoch=None, seq_list=None):
    assert seq_list is None
    super(LmDataset, self).init_seq_order(epoch=epoch)
    epoch = epoch or 1
    self.orths_epoch = self.orths[
                       len(self.orths) * (epoch % self.partition_epoch) // self.partition_epoch:
                       len(self.orths) * ((epoch % self.partition_epoch) + 1) // self.partition_epoch]
    self.seq_order = self.get_seq_order_for_epoch(
      epoch=epoch, num_seqs=len(self.orths_epoch), get_seq_len=lambda i: len(self.orths_epoch[i]))
    self.next_orth_idx = 0
    self.next_seq_idx = 0
    self.num_skipped = 0
    self.num_unknown = 0
    if self.seq_gen:
      self.seq_gen.random_seed(epoch)
    return True

  def _reduce_log_skipped_seqs(self):
    if isinstance(self.log_skipped_seqs, bool):
      return
    assert isinstance(self.log_skipped_seqs, int)
    assert self.log_skipped_seqs >= 1
    self.log_skipped_seqs -= 1
    if not self.log_skipped_seqs:
      print("LmDataset: will stop logging about skipped sequences now", file=log.v4)

  def _reduce_log_auto_replace_unknown_symbols(self):
    if isinstance(self.log_auto_replace_unknown_symbols, bool):
      return
    assert isinstance(self.log_auto_replace_unknown_symbols, int)
    assert self.log_auto_replace_unknown_symbols >= 1
    self.log_auto_replace_unknown_symbols -= 1
    if not self.log_auto_replace_unknown_symbols:
      print("LmDataset: will stop logging about auto-replace with unknown symbol now", file=log.v4)

  def _collect_single_seq(self, seq_idx):
    """
    :type seq_idx: int
    :rtype: DatasetSeq | None
    :returns DatasetSeq or None if seq_idx >= num_seqs.
    """
    while True:
      if self.next_orth_idx >= len(self.orths_epoch):
        assert self.next_seq_idx <= seq_idx, "We expect that we iterate through all seqs."
        if self.num_skipped > 0:
          print("LmDataset: reached end, skipped %i sequences" % self.num_skipped)
        return None
      assert self.next_seq_idx == seq_idx, "We expect that we iterate through all seqs."
      orth = self.orths_epoch[self.seq_order[self.next_orth_idx]]
      self.next_orth_idx += 1
      if orth == "</s>": continue  # special sentence end symbol. empty seq, ignore.

      if self.seq_gen:
        try:
          phones = self.seq_gen.generate_seq(orth)
        except KeyError as e:
          if self.log_skipped_seqs:
            print("LmDataset: skipping sequence %r because of missing lexicon entry: %s" % (orth, e), file=log.v4)
            self._reduce_log_skipped_seqs()
          if self.error_on_invalid_seq:
            raise Exception("LmDataset: invalid seq %r, missing lexicon entry %r" % (orth, e))
          self.num_skipped += 1
          continue  # try another seq
        data = self.seq_gen.seq_to_class_idxs(phones, dtype=self.dtype)

      elif self.orth_symbols:
        orth_syms = parse_orthography(orth, **self.parse_orth_opts)
        while True:
          orth_syms = sum([self.orth_replace_map.get(s, [s]) for s in orth_syms], [])
          i = 0
          while i < len(orth_syms) - 1:
            if orth_syms[i:i+2] == [" ", " "]:
              orth_syms[i:i+2] = [" "]  # collapse two spaces
            else:
              i += 1
          if self.auto_replace_unknown_symbol:
            try:
              map(self.orth_symbols_map.__getitem__, orth_syms)
            except KeyError as e:
              orth_sym = e.message
              if self.log_auto_replace_unknown_symbols:
                print("LmDataset: unknown orth symbol %r, adding to orth_replace_map as %r" % (orth_sym, self.unknown_symbol), file=log.v3)
                self._reduce_log_auto_replace_unknown_symbols()
              self.orth_replace_map[orth_sym] = [self.unknown_symbol] if self.unknown_symbol is not None else []
              continue  # try this seq again with updated orth_replace_map
          break
        self.num_unknown += orth_syms.count(self.unknown_symbol)
        if self.word_based:
          orth_debug_str = repr(orth_syms)
        else:
          orth_debug_str = repr("".join(orth_syms))
        try:
          data = numpy.array(map(self.orth_symbols_map.__getitem__, orth_syms), dtype=self.dtype)
        except KeyError as e:
          if self.log_skipped_seqs:
            print("LmDataset: skipping sequence %s because of missing orth symbol: %s" % (orth_debug_str, e), file=log.v4)
            self._reduce_log_skipped_seqs()
          if self.error_on_invalid_seq:
            raise Exception("LmDataset: invalid seq %s, missing orth symbol %s" % (orth_debug_str, e))
          self.num_skipped += 1
          continue  # try another seq

      else:
        assert False

      targets = {}
      for i in range(self.add_random_phone_seqs):
        assert self.seq_gen  # not implemented atm for orths
        phones = self.seq_gen.generate_garbage_seq(target_len=data.shape[0])
        targets["random%i" % i] = self.seq_gen.seq_to_class_idxs(phones, dtype=self.dtype)
      if self.add_delayed_seq_data:
        targets["delayed"] = numpy.concatenate(
          ([self.orth_symbols_map[self.delayed_seq_data_start_symbol]], data[:-1])).astype(self.dtype)
        assert targets["delayed"].shape == data.shape
      self.next_seq_idx = seq_idx + 1
      return DatasetSeq(seq_idx=seq_idx, features=data, targets=targets)


def _is_bliss(filename):
  try:
    corpus_file = open(filename, 'rb')
    if filename.endswith(".gz"):
      corpus_file = gzip.GzipFile(fileobj=corpus_file)
    context = iter(etree.iterparse(corpus_file, events=('start', 'end')))
    _, root = next(context)  # get root element
    return True
  except IOError:  # 'Not a gzipped file' or so
    pass
  except etree.ParseError:  # 'syntax error' or so
    pass
  return False


def _iter_bliss(filename, callback):
  corpus_file = open(filename, 'rb')
  if filename.endswith(".gz"):
    corpus_file = gzip.GzipFile(fileobj=corpus_file)

  def getelements(tag):
    """Yield *tag* elements from *filename_or_file* xml incrementally."""
    context = iter(etree.iterparse(corpus_file, events=('start', 'end')))
    _, root = next(context) # get root element
    tree = [root]
    for event, elem in context:
      if event == "start":
        tree += [elem]
      elif event == "end":
        assert tree[-1] is elem
        tree = tree[:-1]
      if event == 'end' and elem.tag == tag:
        yield tree, elem
        root.clear()  # free memory

  for tree, elem in getelements("segment"):
    elem_orth = elem.find("orth")
    orth_raw = elem_orth.text  # should be unicode
    orth_split = orth_raw.split()
    orth = " ".join(orth_split)

    callback(orth)


def _iter_txt(filename, callback):
  f = open(filename, 'rb')
  if filename.endswith(".gz"):
    f = gzip.GzipFile(fileobj=f)

  for l in f:
    try:
      l = l.decode("utf8")
    except UnicodeDecodeError:
      l = l.decode("latin_1")  # or iso8859_15?
    l = l.strip()
    if not l: continue
    callback(l)


class AllophoneState:
  # In Sprint, see AllophoneStateAlphabet::index().
  id = None  # u16 in Sprint. here just str
  context_history = ()  # list[u16] of phone id. here just list[str]
  context_future = ()  # list[u16] of phone id. here just list[str]
  boundary = 0  # s16. flags. 1 -> initial (@i), 2 -> final (@f)
  state = None  # s16, e.g. 0,1,2
  _attrs = ["id", "context_history", "context_future", "boundary", "state"]

  def __init__(self, id=None, state=None):
    """
    :param str id: phone
    :param int|None state:
    """
    self.id = id
    self.state = state

  def format(self):
    s = "%s{%s+%s}" % (
      self.id,
      "-".join(self.context_history) or "#",
      "-".join(self.context_future) or "#")
    if self.boundary & 1:
      s += "@i"
    if self.boundary & 2:
      s += "@f"
    if self.state is not None:
      s += ".%i" % self.state
    return s

  def __repr__(self):
    return self.format()

  def mark_initial(self):
    self.boundary = self.boundary | 1

  def mark_final(self):
    self.boundary = self.boundary | 2

  def phoneme(self, ctx_offset, out_of_context_id=None):
    """

    Phoneme::Id ContextPhonology::PhonemeInContext::phoneme(s16 pos) const {
      if (pos == 0)
        return phoneme_;
      else if (pos > 0) {
        if (u16(pos - 1) < context_.future.length())
          return context_.future[pos - 1];
        else
          return Phoneme::term;
      } else { verify(pos < 0);
        if (u16(-1 - pos) < context_.history.length())
          return context_.history[-1 - pos];
        else
          return Phoneme::term;
      }
    }

    :param int ctx_offset: 0 for center, >0 for future, <0 for history
    :param str|None out_of_context_id: what to return out of our context
    :return: phone-id from the offset
    :rtype: str
    """
    if ctx_offset == 0:
      return self.id
    if ctx_offset > 0:
      idx = ctx_offset - 1
      if idx >= len(self.context_future):
        return out_of_context_id
      return self.context_future[idx]
    if ctx_offset < 0:
      idx = -ctx_offset - 1
      if idx >= len(self.context_history):
        return out_of_context_id
      return self.context_history[idx]
    assert False

  def set_phoneme(self, ctx_offset, phone_id):
    """
    :param int ctx_offset: 0 for center, >0 for future, <0 for history
    :param str phone_id:
    """
    if ctx_offset == 0:
      self.id = phone_id
    elif ctx_offset > 0:
      idx = ctx_offset - 1
      assert idx == len(self.context_future)
      self.context_future = self.context_future + (phone_id,)
    elif ctx_offset < 0:
      idx = -ctx_offset - 1
      assert idx == len(self.context_history)
      self.context_history = self.context_history + (phone_id,)

  def phone_idx(self, ctx_offset, phone_idxs):
    """
    :param int ctx_offset: see self.phoneme()
    :param dict[str,int] phone_idxs:
    :rtype: int
    """
    phone = self.phoneme(ctx_offset=ctx_offset)
    if phone is None:
      return 0  # by definition in the Sprint C++ code: static const Id term = 0;
    else:
      return phone_idxs[phone]

  def index(self, phone_idxs, num_states=3, context_length=1):
    """
    Original Sprint C++ code:

        AllophoneStateAlphabet::Index AllophoneStateAlphabet::index(const AllophoneState &phone) const {
          require(nStates_ && contextLength_);
          require(phone.boundary < 4);
          require(0 <= phone.state && phone.state < (s16)nStates_);
          u32 result = 0;
          for (s32 i = - contextLength_; i <= s32(contextLength_); ++i) {
            result *= pi_->nPhonemes() + 1;
            result += phone.phoneme(i);
          }
          result *= 4;
          result += phone.boundary;
          result *= nStates_;
          result += phone.state;
          ensure(result < nClasses_);
          return result + 1;
        }

    :param dict[str,int] phone_idxs:
    :param int num_states: how much state per allophone
    :param int context_length: how much left/right context
    :rtype: int
    """
    assert max(len(self.context_history), len(self.context_future)) <= context_length
    assert 0 <= self.boundary < 4
    assert 0 <= self.state < num_states
    num_phones = max(phone_idxs.values()) + 1
    result = 0
    for i in range(-context_length, context_length + 1):
      result *= num_phones + 1
      result += self.phone_idx(ctx_offset=i, phone_idxs=phone_idxs)
    result *= 4
    result += self.boundary
    result *= num_states
    result += self.state
    return result + 1

  @classmethod
  def from_index(cls, index, phone_ids, num_states=3, context_length=1):
    """
    Original Sprint C++ code:

        AllophoneState AllophoneStateAlphabet::allophoneState(AllophoneStateAlphabet::Index in) const {
          require(nStates_ && contextLength_);
          require(in != Fsa::Epsilon);
          require(in < nClasses_);
          AllophoneState result;
          Index code = in - 1;
          result.state    = code % nStates_; code /= nStates_;
          result.boundary = code % 4;        code /= 4;
          for (s32 i = contextLength_; i >= - s32(contextLength_); --i) {
            result.setPhoneme(i, code % (pi_->nPhonemes() + 1));
            code /= pi_->nPhonemes() + 1;
          }
          ensure_(index(result) == in);
          return result;
        }

    :param int index:
    :param dict[int,str] phone_ids: reverse-map from self.index(). idx -> id
    :param int num_states: how much state per allophone
    :param int context_length: how much left/right context
    :rtype: int
    :rtype: AllophoneState
    """
    num_phones = max(phone_ids.keys()) + 1
    code = index - 1
    result = AllophoneState()
    result.state = code % num_states
    code //= num_states
    result.boundary = code % 4
    code //= 4
    for i in reversed(range(-context_length, context_length + 1)):
      phone_idx = code % (num_phones + 1)
      code //= num_phones + 1
      result.set_phoneme(ctx_offset=i, phone_id=phone_ids[phone_idx])
    return result

  def __hash__(self):
    return hash(tuple([getattr(self, a) for a in self._attrs]))

  def __eq__(self, other):
    for a in self._attrs:
      if getattr(self, a) != getattr(other, a):
        return False
    return True

  def __ne__(self, other):
    return not self == other


class Lexicon:

  def __init__(self, filename):
    print("Loading lexicon", filename, file=log.v4)
    lex_file = open(filename, 'rb')
    if filename.endswith(".gz"):
      lex_file = gzip.GzipFile(fileobj=lex_file)
    self.phoneme_list = []  # type: list[str]
    self.phonemes = {}  # type: dict[str,dict[str]]  # phone -> {index, symbol, variation}
    self.lemmas = {}  # type: dict[str,dict[str]]  # orth -> {orth, phons}

    context = iter(etree.iterparse(lex_file, events=('start', 'end')))
    _, root = next(context)  # get root element
    tree = [root]
    for event, elem in context:
      if event == "start":
        tree += [elem]
      elif event == "end":
        assert tree[-1] is elem
        tree = tree[:-1]
        if elem.tag == "phoneme":
          symbol = elem.find("symbol").text.strip()  # should be unicode
          assert isinstance(symbol, (str, unicode))
          if elem.find("variation") is not None:
            variation = elem.find("variation").text.strip()
          else:
            variation = "context"  # default
          assert symbol not in self.phonemes
          assert variation in ["context", "none"]
          self.phoneme_list.append(symbol)
          self.phonemes[symbol] = {"index": len(self.phonemes), "symbol": symbol, "variation": variation}
          root.clear()  # free memory
        elif elem.tag == "phoneme-inventory":
          print("Finished phoneme inventory, %i phonemes" % len(self.phonemes), file=log.v4)
          root.clear()  # free memory
        elif elem.tag == "lemma":
          for orth_elem in elem.findall("orth"):
            orth = (orth_elem.text or "").strip()
            phons = [{"phon": e.text.strip(), "score": float(e.attrib.get("score", 0))} for e in elem.findall("phon")]
            assert orth not in self.lemmas
            self.lemmas[orth] = {"orth": orth, "phons": phons}
          root.clear()  # free memory
    print("Finished whole lexicon, %i lemmas" % len(self.lemmas), file=log.v4)


class StateTying:
  def __init__(self, state_tying_file):
    self.allo_map = {}  # allophone-state-str -> class-idx
    self.class_map = {}  # class-idx -> set(allophone-state-str)
    ls = open(state_tying_file).read().splitlines()
    for l in ls:
      allo_str, class_idx_str = l.split()
      class_idx = int(class_idx_str)
      assert allo_str not in self.allo_map
      self.allo_map[allo_str] = class_idx
      self.class_map.setdefault(class_idx, set()).add(allo_str)
    min_class_idx = min(self.class_map.keys())
    max_class_idx = max(self.class_map.keys())
    assert min_class_idx == 0
    assert max_class_idx == len(self.class_map) - 1, "some classes are not represented"
    self.num_classes = len(self.class_map)


class PhoneSeqGenerator:
  def __init__(self, lexicon_file,
               allo_num_states=3, allo_context_len=1,
               state_tying_file=None,
               add_silence_beginning=0.1, add_silence_between_words=0.1, add_silence_end=0.1,
               repetition=0.9, silence_repetition=0.95):
    """
    :param str lexicon_file: lexicon XML file
    :param int allo_num_states: how much HMM states per allophone (all but silence)
    :param int allo_context_len: how much context to store left and right. 1 -> triphone
    :param str | None state_tying_file: for state-tying, if you want that
    :param float add_silence_beginning: prob of adding silence at beginning
    :param float add_silence_between_words: prob of adding silence between words
    :param float add_silence_end: prob of adding silence at end
    :param float repetition: prob of repeating an allophone
    :param float silence_repetition: prob of repeating the silence allophone
    """
    self.lexicon = Lexicon(lexicon_file)
    self.phonemes = sorted(self.lexicon.phonemes.keys(), key=lambda s: self.lexicon.phonemes[s]["index"])
    self.rnd = Random(0)
    self.allo_num_states = allo_num_states
    self.allo_context_len = allo_context_len
    self.add_silence_beginning = add_silence_beginning
    self.add_silence_between_words = add_silence_between_words
    self.add_silence_end = add_silence_end
    self.repetition = repetition
    self.silence_repetition = silence_repetition
    self.si_lemma = self.lexicon.lemmas["[SILENCE]"]
    self.si_phone = self.si_lemma["phons"][0]["phon"]
    if state_tying_file:
      self.state_tying = StateTying(state_tying_file)
    else:
      self.state_tying = None

  def random_seed(self, seed):
    self.rnd.seed(seed)

  def get_class_labels(self):
    if self.state_tying:
      # State tying labels. Represented by some allophone state str.
      return ["|".join(sorted(self.state_tying.class_map[i])) for i in range(self.state_tying.num_classes)]
    else:
      # The phonemes are the labels.
      return self.phonemes

  def seq_to_class_idxs(self, phones, dtype=None):
    """
    :param list[AllophoneState] phones: list of allophone states
    :param str dtype: eg "int32"
    :rtype: numpy.ndarray
    :returns 1D numpy array with the indices
    """
    if dtype is None: dtype = "int32"
    if self.state_tying:
      # State tying indices.
      return numpy.array([self.state_tying.allo_map[a.format()] for a in phones], dtype=dtype)
    else:
      # Phoneme indices. This must be consistent with get_class_labels.
      # It should not happen that we don't have some phoneme. The lexicon should not be inconsistent.
      return numpy.array([self.lexicon.phonemes[p.id]["index"] for p in phones], dtype=dtype)

  def _iter_orth(self, orth):
    if self.rnd.random() < self.add_silence_beginning:
      yield self.si_lemma
    symbols = list(orth.split())
    i = 0
    while i < len(symbols):
      symbol = symbols[i]
      try:
        lemma = self.lexicon.lemmas[symbol]
      except KeyError:
        if "/" in symbol:
          symbols[i:i+1] = symbol.split("/")
          continue
        if "-" in symbol:
          symbols[i:i+1] = symbol.split("-")
          continue
        raise
      i += 1
      yield lemma
      if i < len(symbols):
        if self.rnd.random() < self.add_silence_between_words:
          yield self.si_lemma
    if self.rnd.random() < self.add_silence_end:
      yield self.si_lemma

  def orth_to_phones(self, orth):
    phones = []
    for lemma in self._iter_orth(orth):
      phon = self.rnd.choice(lemma["phons"])
      phones += [phon["phon"]]
    return " ".join(phones)

  def _phones_to_allos(self, phones):
    for p in phones:
      a = AllophoneState()
      a.id = p
      yield a

  def _random_allo_silence(self, phone=None):
    if phone is None: phone = self.si_phone
    while True:
      a = AllophoneState()
      a.id = phone
      a.mark_initial()
      a.mark_final()
      a.state = 0  # silence only has one state
      yield a
      if self.rnd.random() >= self.silence_repetition:
        break

  def _allos_add_states(self, allos):
    for _a in allos:
      if _a.id == self.si_phone:
        for a in self._random_allo_silence(_a.id):
          yield a
      else:  # non-silence
        for state in range(self.allo_num_states):
          while True:
            a = AllophoneState()
            a.id = _a.id
            a.context_history = _a.context_history
            a.context_future = _a.context_future
            a.boundary = _a.boundary
            a.state = state
            yield a
            if self.rnd.random() >= self.repetition:
              break

  def _allos_set_context(self, allos):
    if self.allo_context_len == 0: return
    ctx = []
    for a in allos:
      if self.lexicon.phonemes[a.id]["variation"] == "context":
        a.context_history = tuple(ctx)
        ctx += [a.id]
        ctx = ctx[-self.allo_context_len:]
      else:
        ctx = []
    ctx = []
    for a in reversed(allos):
      if self.lexicon.phonemes[a.id]["variation"] == "context":
        a.context_future = tuple(reversed(ctx))
        ctx += [a.id]
        ctx = ctx[-self.allo_context_len:]
      else:
        ctx = []

  def generate_seq(self, orth):
    """
    :param str orth: orthography as a str. orth.split() should give words in the lexicon
    :rtype: list[AllophoneState]
    :returns allophone state list. those will have repetitions etc
    """
    allos = []
    for lemma in self._iter_orth(orth):
      phon = self.rnd.choice(lemma["phons"])
      l_allos = list(self._phones_to_allos(phon["phon"].split()))
      l_allos[0].mark_initial()
      l_allos[-1].mark_final()
      allos += l_allos
    self._allos_set_context(allos)
    allos = list(self._allos_add_states(allos))
    return allos

  def _random_phone_seq(self, prob_add=0.8):
    while True:
      yield self.rnd.choice(self.phonemes)
      if self.rnd.random() >= prob_add:
        break

  def _random_allo_seq(self, prob_word_add=0.8):
    allos = []
    while True:
      phones = self._random_phone_seq()
      w_allos = list(self._phones_to_allos(phones))
      w_allos[0].mark_initial()
      w_allos[-1].mark_final()
      allos += w_allos
      if self.rnd.random() >= prob_word_add:
        break
    self._allos_set_context(allos)
    return list(self._allos_add_states(allos))

  def generate_garbage_seq(self, target_len):
    """
    :param int target_len: len of the returned seq
    :rtype: list[AllophoneState]
    :returns allophone state list. those will have repetitions etc.
    It will randomly generate a sequence of phonemes and transform that
    into a list of allophones in a similar way than generate_seq().
    """
    allos = []
    while True:
      allos += self._random_allo_seq()
      # Add some silence so that left/right context is correct for further allophones.
      allos += list(self._random_allo_silence())
      if len(allos) >= target_len:
        allos = allos[:target_len]
        break
    return allos


class _TFKerasDataset(CachedDataset2):
  """
  Wraps around any dataset from tf.contrib.keras.datasets.
  See: https://www.tensorflow.org/versions/master/api_docs/python/tf/contrib/keras/datasets
  TODO: Should maybe be moved to a separate file. (Only here because of tf.contrib.keras.datasets.reuters).
  """
  # TODO...


class _NltkCorpusReaderDataset(CachedDataset2):
  """
  Wraps around any dataset from nltk.corpus.
  TODO: Should maybe be moved to a separate file, e.g. CorpusReaderDataset.py or so?
  """
  # TODO ...


class TranslationDataset(CachedDataset2):
  """
  Based on the conventions by our team for translation datasets.
  It gets a directory and expects these files:

      source.dev(.gz)?
      source.train(.gz)?
      source.vocab.pkl
      target.dev(.gz)?
      target.train(.gz)?
      target.vocab.pkl
  """

  MapToDataKeys = {"source": "data", "target": "classes"}  # just by our convention

  def __init__(self, path, postfix, partition_epoch=None, **kwargs):
    """
    :param str path: the directory containing the files
    :param str postfix: e.g. "train" or "dev". it will then search for "source." + postfix and "target." + postfix.
    :param bool random_shuffle_epoch1: if True, will also randomly shuffle epoch 1. see self.init_seq_order().
    :param int partition_epoch: if provided, will partition the dataset into multiple epochs
    """
    super(TranslationDataset, self).__init__(**kwargs)
    self.path = path
    self.postfix = postfix
    self.partition_epoch = partition_epoch
    from threading import Lock, Thread
    self._lock = Lock()
    self._partition_epoch_num_seqs = []
    import os
    assert os.path.isdir(path)
    self._data_files = {data_key: self._get_data_file(prefix) for (prefix, data_key) in self.MapToDataKeys.items()}
    self._data = {data_key: [] for data_key in self._data_files.keys()}  # type: dict[str,list[numpy.ndarray]]
    self._data_len = None  # type: int|None
    self._vocabs = {data_key: self._get_vocab(prefix) for (prefix, data_key) in self.MapToDataKeys.items()}
    self.num_outputs = {k: [max(self._vocabs[k].values()) + 1, 1] for k in self._vocabs.keys()}  # all sparse
    assert all([v1 <= 2 ** 31 for (k, (v1, v2)) in self.num_outputs.items()])  # we use int32
    self.num_inputs = self.num_outputs["data"][0]
    self._reversed_vocabs = {k: self._reverse_vocab(k) for k in self._vocabs.keys()}
    self.labels = {k: self._get_label_list(k) for k in self._vocabs.keys()}
    self._seq_order = None  # type: None|list[int]  # seq_idx -> line_nr
    self._thread = Thread(name="%r reader" % self, target=self._thread_main)
    self._thread.daemon = True
    self._thread.start()

  def _thread_main(self):
    from Util import interrupt_main
    try:
      import better_exchook
      better_exchook.install()
      from Util import AsyncThreadRun

      sources_async = AsyncThreadRun(
        name="%r: read source data", func=lambda: self._read_data(self._data_files["data"]))
      targets_async = AsyncThreadRun(
        name="%r: read target data", func=lambda: self._read_data(self._data_files["classes"]))
      sources = sources_async.get()
      with self._lock:
        self._data_len = len(sources)
      targets = targets_async.get()
      assert len(targets) == self._data_len, "len of source is %r != len of target %r" % (
        self._data_len, len(targets))
      for k, f in list(self._data_files.items()):
        f.close()
        self._data_files[k] = None
      data_strs = {"data": sources, "classes": targets}
      ChunkSize = 1000
      i = 0
      while i < self._data_len:
        for k in ("data", "classes"):
          vocab = self._vocabs[k]
          data = [self._data_str_to_numpy(vocab, s) for s in data_strs[k][i:i + ChunkSize]]
          with self._lock:
            self._data[k].extend(data)
        i += ChunkSize

    except Exception:
      sys.excepthook(*sys.exc_info())
      interrupt_main()

  def _get_data_file(self, prefix):
    """
    :param str prefix: e.g. "source" or "target"
    :return: full filename
    :rtype: io.FileIO
    """
    import os
    filename = "%s/%s.%s" % (self.path, prefix, self.postfix)
    if os.path.exists(filename):
      return open(filename, "rb")
    if os.path.exists(filename + ".gz"):
      import gzip
      return gzip.GzipFile(filename + ".gz", "rb")
    raise Exception("Data file not found: %r (.gz)?" % filename)

  def _get_vocab(self, prefix):
    """
    :param str prefix: e.g. "source" or "target"
    :rtype: dict[str,int]
    """
    import os
    filename = "%s/%s.vocab.pkl" % (self.path, prefix)
    if not os.path.exists(filename):
      raise Exception("Vocab file not found: %r" % filename)
    import pickle
    vocab = pickle.load(open(filename, "rb"))
    assert isinstance(vocab, dict)
    return vocab

  def _reverse_vocab(self, data_key):
    """
    Note that there might be multiple items in the vocabulary (e.g. "<S>" and "</S>")
    which map to the same label index.
    We sort the list by lexical order and the last entry for a particular label index is used ("<S>" in that example).

    :param str data_key: e.g. "data" or "classes"
    :rtype: dict[int,str]
    """
    return {v: k for (k, v) in sorted(self._vocabs[data_key].items())}

  def _get_label_list(self, data_key):
    """
    :param str data_key: e.g. "data" or "classes"
    :return: list of len num labels
    :rtype: list[str]
    """
    reversed_vocab = self._reversed_vocabs[data_key]
    assert isinstance(reversed_vocab, dict)
    num_labels = self.num_outputs[data_key][0]
    return list(map(reversed_vocab.__getitem__, range(num_labels)))

  @staticmethod
  def _read_data(f):
    """
    :param io.FileIO f: file
    """
    data = []
    assert isinstance(data, list)
    while True:
      # Read in chunks. This can speed it up.
      ls = f.readlines(10000)
      data.extend([l.decode("utf8").strip() for l in ls])
      if not ls:
        break
    return data

  @staticmethod
  def _data_str_to_numpy(vocab, s):
    """
    :param dict[str,int] vocab:
    :param str s:
    :rtype: numpy.ndarray
    """
    words = s.split()
    return numpy.array(list(map(vocab.__getitem__, words)), dtype=numpy.int32)

  def _get_data(self, key, line_nr):
    """
    :param str key: "data" or "classes"
    :param int line_nr:
    :return: 1D array
    :rtype: numpy.ndarray
    """
    import time
    last_len = None
    while True:
      with self._lock:
        if self._data_len is not None:
          assert line_nr <= self._data_len
        cur_len = len(self._data[key])
        if line_nr < cur_len:
          return self._data[key][line_nr]
      if cur_len != last_len:
        print("%r: waiting for %r, line %i (%i loaded so far)..." % (self, key, line_nr, cur_len), file=log.v3)
      last_len = cur_len
      time.sleep(1)

  def _get_data_len(self):
    """
    :rtype: num seqs of the whole underlying data
    :rtype: int
    """
    import time
    t = 0
    while True:
      with self._lock:
        if self._data_len is not None:
          return self._data_len
      if t == 0:
        print("%r: waiting for data length info..." % (self,), file=log.v3)
      time.sleep(1)
      t += 1

  def _get_line_nr(self, seq_idx):
    """
    :param int seq_idx:
    :return: line-nr, i.e. index in any of the lists `self.data[key]`
    :rtype: int
    """
    if self.partition_epoch:
      epoch = self.epoch or 1
      assert self._partition_epoch_num_seqs
      for n in self._partition_epoch_num_seqs[:(epoch - 1) % self.partition_epoch]:
        seq_idx += n
    if self._seq_order is None:
      return seq_idx
    return self._seq_order[seq_idx]

  def is_data_sparse(self, key):
    return True  # all is sparse

  def get_data_dtype(self, key):
    return "int32"  # sparse -> label idx

  def init_seq_order(self, epoch=None, seq_list=None):
    """
    If random_shuffle_epoch1, for epoch 1 with "random" ordering, we leave the given order as is.
    Otherwise, this is mostly the default behavior.

    :param int|None epoch:
    :param list[str] | None seq_list: In case we want to set a predefined order.
    """
    super(TranslationDataset, self).init_seq_order(epoch=epoch, seq_list=seq_list)
    if not epoch:
      epoch = 1
    if self.partition_epoch:
      epoch = (epoch - 1) // self.partition_epoch + 1  # count starting from epoch 1
    if seq_list is not None:
      self._seq_order = list(seq_list)
      self._num_seqs = len(self._seq_order)
    else:
      num_seqs = self._get_data_len()
      self._seq_order = self.get_seq_order_for_epoch(
        epoch=epoch, num_seqs=num_seqs, get_seq_len=lambda i: len(self._get_data(key="data", line_nr=i)))
      self._num_seqs = num_seqs
    if self.partition_epoch:
      self._partition_epoch_num_seqs = [self._num_seqs // self.partition_epoch] * self.partition_epoch
      i = 0
      while sum(self._partition_epoch_num_seqs) < self._num_seqs:
        self._partition_epoch_num_seqs[i] += 1
        i += 1
        assert i < self.partition_epoch
      assert sum(self._partition_epoch_num_seqs) == self._num_seqs
      self._num_seqs = self._partition_epoch_num_seqs[(self.epoch - 1) % self.partition_epoch]

  def _collect_single_seq(self, seq_idx):
    if seq_idx >= self._num_seqs:
      return None
    line_nr = self._get_line_nr(seq_idx)
    features = self._get_data(key="data", line_nr=line_nr)
    targets = self._get_data(key="classes", line_nr=line_nr)
    assert features is not None and targets is not None
    return DatasetSeq(
      seq_idx=seq_idx,
      seq_tag="line-%i" % line_nr,
      features=features,
      targets=targets)


def _main(argv):
  import better_exchook
  better_exchook.install()
  log.initialize(verbosity=[5])
  print("LmDataset demo startup")
  kwargs = eval(argv[0])
  print("Creating LmDataset with kwargs=%r ..." % kwargs)
  dataset = LmDataset(**kwargs)
  print("init_seq_order ...")
  dataset.init_seq_order(epoch=1)

  seq_idx = 0
  last_log_time = time.time()
  print("start iterating through seqs ...")
  while dataset.is_less_than_num_seqs(seq_idx):
    if seq_idx == 0:
      print("load_seqs with seq_idx=%i ...." % seq_idx)
    dataset.load_seqs(seq_idx, seq_idx + 1)

    if time.time() - last_log_time > 2.0:
      last_log_time = time.time()
      print("Loading %s progress, %i/%i (%.0f%%) seqs loaded (%.0f%% skipped), (%.0f%% unknown) total syms %i ..." % (
            dataset.__class__.__name__, dataset.next_orth_idx, dataset.estimated_num_seqs,
            100.0 * dataset.next_orth_idx / dataset.estimated_num_seqs,
            100.0 * dataset.num_skipped / (dataset.next_orth_idx or 1),
            100.0 * dataset.num_unknown / dataset._num_timesteps_accumulated["data"],
            dataset._num_timesteps_accumulated["data"]))

    seq_idx += 1

  print("finished iterating, num seqs: %i" % seq_idx)
  print("dataset len:", dataset.len_info())


if __name__ == "__main__":
  _main(sys.argv[1:])
