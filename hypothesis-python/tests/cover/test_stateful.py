# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Copyright the Hypothesis Authors.
# Individual contributors are listed in AUTHORS.rst and the git log.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.

import base64
from collections import defaultdict

import pytest
from _pytest.outcomes import Failed, Skipped
from pytest import raises

from hypothesis import __version__, reproduce_failure, seed, settings as Settings
from hypothesis.control import current_build_context
from hypothesis.database import ExampleDatabase
from hypothesis.errors import DidNotReproduce, Flaky, InvalidArgument, InvalidDefinition
from hypothesis.internal.entropy import deterministic_PRNG
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
    run_state_machine_as_test,
)
from hypothesis.strategies import binary, data, integers, just, lists

from tests.common.utils import capture_out, validate_deprecation
from tests.nocover.test_stateful import DepthMachine

NO_BLOB_SETTINGS = Settings(print_blob=False)


class MultipleRulesSameFuncMachine(RuleBasedStateMachine):
    def myfunc(self, data):
        print(data)

    rule1 = rule(data=just("rule1data"))(myfunc)
    rule2 = rule(data=just("rule2data"))(myfunc)


class PreconditionMachine(RuleBasedStateMachine):
    num = 0

    @rule()
    def add_one(self):
        self.num += 1

    @rule()
    def set_to_zero(self):
        self.num = 0

    @rule(num=integers())
    @precondition(lambda self: self.num != 0)
    def div_by_precondition_after(self, num):
        self.num = num / self.num

    @precondition(lambda self: self.num != 0)
    @rule(num=integers())
    def div_by_precondition_before(self, num):
        self.num = num / self.num


TestPrecondition = PreconditionMachine.TestCase
TestPrecondition.settings = Settings(TestPrecondition.settings, max_examples=10)


def test_picks_up_settings_at_first_use_of_testcase():
    assert TestPrecondition.settings.max_examples == 10


def test_multiple_rules_same_func():
    test_class = MultipleRulesSameFuncMachine.TestCase
    with capture_out() as o:
        test_class().runTest()
    output = o.getvalue()
    assert "rule1data" in output
    assert "rule2data" in output


def test_can_get_test_case_off_machine_instance():
    assert DepthMachine().TestCase is DepthMachine().TestCase
    assert DepthMachine().TestCase is not None


class FlakyDrawLessMachine(RuleBasedStateMachine):
    @rule(d=data())
    def action(self, d):
        if current_build_context().is_final:
            d.draw(binary(min_size=1, max_size=1))
        else:
            buffer = binary(min_size=1024, max_size=1024)
            assert 0 not in buffer


def test_flaky_draw_less_raises_flaky():
    with raises(Flaky):
        FlakyDrawLessMachine.TestCase().runTest()


class FlakyStateMachine(RuleBasedStateMachine):
    @rule()
    def action(self):
        assert current_build_context().is_final


def test_flaky_raises_flaky():
    with raises(Flaky):
        FlakyStateMachine.TestCase().runTest()


class FlakyRatchettingMachine(RuleBasedStateMachine):
    ratchet = 0

    @rule(d=data())
    def action(self, d):
        FlakyRatchettingMachine.ratchet += 1
        n = FlakyRatchettingMachine.ratchet
        d.draw(lists(integers(), min_size=n, max_size=n))
        raise AssertionError


class MachineWithConsumingRule(RuleBasedStateMachine):
    b1 = Bundle("b1")
    b2 = Bundle("b2")

    def __init__(self):
        self.created_counter = 0
        self.consumed_counter = 0
        super().__init__()

    @invariant()
    def bundle_length(self):
        assert len(self.bundle("b1")) == self.created_counter - self.consumed_counter

    @rule(target=b1)
    def populate_b1(self):
        self.created_counter += 1
        return self.created_counter

    @rule(target=b2, consumed=consumes(b1))
    def depopulate_b1(self, consumed):
        self.consumed_counter += 1
        return consumed

    @rule(consumed=lists(consumes(b1)))
    def depopulate_b1_multiple(self, consumed):
        self.consumed_counter += len(consumed)

    @rule(value1=b1, value2=b2)
    def check(self, value1, value2):
        assert value1 != value2


TestMachineWithConsumingRule = MachineWithConsumingRule.TestCase


def test_multiple():
    none = multiple()
    some = multiple(1, 2.01, "3", b"4", 5)
    assert len(none.values) == 0 and len(some.values) == 5
    assert set(some.values) == {1, 2.01, "3", b"4", 5}


class MachineUsingMultiple(RuleBasedStateMachine):
    b = Bundle("b")

    def __init__(self):
        self.expected_bundle_length = 0
        super().__init__()

    @invariant()
    def bundle_length(self):
        assert len(self.bundle("b")) == self.expected_bundle_length

    @rule(target=b, items=lists(elements=integers(), max_size=10))
    def populate_bundle(self, items):
        self.expected_bundle_length += len(items)
        return multiple(*items)

    @rule(target=b)
    def do_not_populate(self):
        return multiple()


TestMachineUsingMultiple = MachineUsingMultiple.TestCase


def test_multiple_variables_printed():
    class ProducesMultiple(RuleBasedStateMachine):
        b = Bundle("b")

        @initialize(target=b)
        def populate_bundle(self):
            return multiple(1, 2)

        @rule()
        def fail_fast(self):
            raise AssertionError

    with capture_out() as o:
        # The state machine must raise an exception for the
        # falsifying example to be printed.
        with raises(AssertionError):
            run_state_machine_as_test(ProducesMultiple)

    # This is tightly coupled to the output format of the step printing.
    # The first line is "Falsifying Example:..." the second is creating
    # the state machine, the third is calling the "initialize" method.
    assignment_line = o.getvalue().split("\n")[2]
    # 'populate_bundle()' returns 2 values, so should be
    # expanded to 2 variables.
    assert assignment_line == "v1, v2 = state.populate_bundle()"

    # Make sure MultipleResult is iterable so the printed code is valid.
    # See https://github.com/HypothesisWorks/hypothesis/issues/2311
    state = ProducesMultiple()
    v1, v2 = state.populate_bundle()
    with raises(AssertionError):
        state.fail_fast()


def test_multiple_variables_printed_single_element():
    # https://github.com/HypothesisWorks/hypothesis/issues/3236
    class ProducesMultiple(RuleBasedStateMachine):
        b = Bundle("b")

        @initialize(target=b)
        def populate_bundle(self):
            return multiple(1)

        @rule(b=b)
        def fail_fast(self, b):
            assert b != 1

    with capture_out() as o, raises(AssertionError):
        run_state_machine_as_test(ProducesMultiple)

    assignment_line = o.getvalue().split("\n")[2]
    assert assignment_line == "(v1,) = state.populate_bundle()"

    state = ProducesMultiple()
    (v1,) = state.populate_bundle()
    state.fail_fast((v1,))  # passes if tuple not unpacked
    with raises(AssertionError):
        state.fail_fast(v1)


def test_no_variables_printed():
    class ProducesNoVariables(RuleBasedStateMachine):
        b = Bundle("b")

        @initialize(target=b)
        def populate_bundle(self):
            return multiple()

        @rule()
        def fail_fast(self):
            raise AssertionError

    with capture_out() as o:
        # The state machine must raise an exception for the
        # falsifying example to be printed.
        with raises(AssertionError):
            run_state_machine_as_test(ProducesNoVariables)

    # This is tightly coupled to the output format of the step printing.
    # The first line is "Falsifying Example:..." the second is creating
    # the state machine, the third is calling the "initialize" method.
    assignment_line = o.getvalue().split("\n")[2]
    # 'populate_bundle()' returns 0 values, so there should be no
    # variable assignment.
    assert assignment_line == "state.populate_bundle()"


def test_consumes_typecheck():
    with pytest.raises(TypeError):
        consumes(integers())


def test_ratchetting_raises_flaky():
    with raises(Flaky):
        FlakyRatchettingMachine.TestCase().runTest()


def test_empty_machine_is_invalid():
    class EmptyMachine(RuleBasedStateMachine):
        pass

    with raises(InvalidDefinition):
        EmptyMachine.TestCase().runTest()


def test_machine_with_no_terminals_is_invalid():
    class NonTerminalMachine(RuleBasedStateMachine):
        @rule(value=Bundle("hi"))
        def bye(self, hi):
            pass

    with raises(InvalidDefinition):
        NonTerminalMachine.TestCase().runTest()


def test_minimizes_errors_in_teardown():
    counter = [0]

    class Foo(RuleBasedStateMachine):
        @initialize()
        def init(self):
            counter[0] = 0

        @rule()
        def increment(self):
            counter[0] += 1

        def teardown(self):
            assert not counter[0]

    with raises(AssertionError):
        run_state_machine_as_test(Foo)
    assert counter[0] == 1


class RequiresInit(RuleBasedStateMachine):
    def __init__(self, threshold):
        super().__init__()
        self.threshold = threshold

    @rule(value=integers())
    def action(self, value):
        if value > self.threshold:
            raise ValueError(f"{value} is too high")


def test_can_use_factory_for_tests():
    with raises(ValueError):
        run_state_machine_as_test(
            lambda: RequiresInit(42), settings=Settings(max_examples=100)
        )


class FailsEventually(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.counter = 0

    @rule()
    def increment(self):
        self.counter += 1
        assert self.counter < 10


FailsEventually.TestCase.settings = Settings(stateful_step_count=5)


def test_can_explicitly_pass_settings():
    run_state_machine_as_test(FailsEventually)
    try:
        FailsEventually.TestCase.settings = Settings(
            FailsEventually.TestCase.settings, stateful_step_count=15
        )
        run_state_machine_as_test(
            FailsEventually, settings=Settings(stateful_step_count=2)
        )
    finally:
        FailsEventually.TestCase.settings = Settings(
            FailsEventually.TestCase.settings, stateful_step_count=5
        )


def test_settings_argument_is_validated():
    with pytest.raises(InvalidArgument):
        run_state_machine_as_test(FailsEventually, settings=object())


def test_runner_that_checks_factory_produced_a_machine():
    with pytest.raises(InvalidArgument):
        run_state_machine_as_test(object)


def test_settings_attribute_is_validated():
    real_settings = FailsEventually.TestCase.settings
    try:
        FailsEventually.TestCase.settings = object()
        with pytest.raises(InvalidArgument):
            run_state_machine_as_test(FailsEventually)
    finally:
        FailsEventually.TestCase.settings = real_settings


def test_saves_failing_example_in_database():
    db = ExampleDatabase(":memory:")
    with raises(AssertionError):
        run_state_machine_as_test(
            DepthMachine, settings=Settings(database=db, max_examples=100)
        )
    assert any(list(db.data.values()))


def test_can_run_with_no_db():
    with deterministic_PRNG(), raises(AssertionError):
        run_state_machine_as_test(
            DepthMachine, settings=Settings(database=None, max_examples=10_000)
        )


def test_stateful_double_rule_is_forbidden(recwarn):
    with pytest.raises(InvalidDefinition):

        class DoubleRuleMachine(RuleBasedStateMachine):
            @rule(num=just(1))
            @rule(num=just(2))
            def whatevs(self, num):
                pass


def test_can_explicitly_call_functions_when_precondition_not_satisfied():
    class BadPrecondition(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()

        @precondition(lambda self: False)
        @rule()
        def test_blah(self):
            raise ValueError()

        @rule()
        def test_foo(self):
            self.test_blah()

    with pytest.raises(ValueError):
        run_state_machine_as_test(BadPrecondition)


def test_invariant():
    """If an invariant raise an exception, the exception is propagated."""

    class Invariant(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()

        @invariant()
        def test_blah(self):
            raise ValueError()

        @rule()
        def do_stuff(self):
            pass

    with pytest.raises(ValueError):
        run_state_machine_as_test(Invariant)


def test_no_double_invariant():
    """The invariant decorator can't be applied multiple times to a single
    function."""
    with raises(InvalidDefinition):

        class Invariant(RuleBasedStateMachine):
            def __init__(self):
                super().__init__()

            @invariant()
            @invariant()
            def test_blah(self):
                pass

            @rule()
            def do_stuff(self):
                pass


def test_invariant_precondition():
    """If an invariant precodition isn't met, the invariant isn't run.

    The precondition decorator can be applied in any order.
    """

    class Invariant(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()

        @invariant()
        @precondition(lambda _: False)
        def an_invariant(self):
            raise ValueError()

        @precondition(lambda _: False)
        @invariant()
        def another_invariant(self):
            raise ValueError()

        @rule()
        def do_stuff(self):
            pass

    run_state_machine_as_test(Invariant)


@pytest.mark.parametrize(
    "decorators",
    [
        (invariant(), rule()),
        (rule(), invariant()),
        (invariant(), initialize()),
        (initialize(), invariant()),
        (invariant(), precondition(lambda self: True), rule()),
        (rule(), precondition(lambda self: True), invariant()),
        (precondition(lambda self: True), invariant(), rule()),
        (precondition(lambda self: True), rule(), invariant()),
    ],
    ids=lambda x: "-".join(f.__qualname__.split(".")[0] for f in x),
)
def test_invariant_and_rule_are_incompatible(decorators):
    """It's an error to apply @invariant and @rule to the same method."""

    def method(self):
        pass

    for d in decorators[:-1]:
        method = d(method)
    with pytest.raises(InvalidDefinition):
        decorators[-1](method)


def test_invalid_rule_argument():
    """Rule kwargs that are not a Strategy are expected to raise an InvalidArgument error."""
    with pytest.raises(InvalidArgument):

        class InvalidRuleMachine(RuleBasedStateMachine):
            @rule(strategy=object())
            def do_stuff(self):
                pass


def test_invalid_initialize_argument():
    """Initialize kwargs that are not a Strategy are expected to raise an InvalidArgument error."""
    with pytest.raises(InvalidArgument):

        class InvalidInitialize(RuleBasedStateMachine):
            @initialize(strategy=object())
            def initialize(self):
                pass


def test_multiple_invariants():
    """If multiple invariants are present, they all get run."""

    class Invariant(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.first_invariant_ran = False

        @invariant()
        def invariant_1(self):
            self.first_invariant_ran = True

        @precondition(lambda self: self.first_invariant_ran)
        @invariant()
        def invariant_2(self):
            raise ValueError()

        @rule()
        def do_stuff(self):
            pass

    with pytest.raises(ValueError):
        run_state_machine_as_test(Invariant)


def test_explicit_invariant_call_with_precondition():
    """Invariants can be called explicitly even if their precondition is not
    satisfied."""

    class BadPrecondition(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()

        @precondition(lambda self: False)
        @invariant()
        def test_blah(self):
            raise ValueError()

        @rule()
        def test_foo(self):
            self.test_blah()

    with pytest.raises(ValueError):
        run_state_machine_as_test(BadPrecondition)


def test_invariant_checks_initial_state_if_no_initialize_rules():
    """Invariants are checked before any rules run."""

    class BadPrecondition(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.num = 0

        @invariant()
        def test_blah(self):
            if self.num == 0:
                raise ValueError()

        @rule()
        def test_foo(self):
            self.num += 1

    with pytest.raises(ValueError):
        run_state_machine_as_test(BadPrecondition)


def test_invariant_failling_present_in_falsifying_example():
    @Settings(print_blob=False)
    class BadInvariant(RuleBasedStateMachine):
        @initialize()
        def initialize_1(self):
            pass

        @invariant()
        def invariant_1(self):
            raise ValueError()

        @rule()
        def rule_1(self):
            pass

    with capture_out() as o:
        with pytest.raises(ValueError):
            run_state_machine_as_test(BadInvariant)

    result = o.getvalue()
    assert (
        result
        == """\
Falsifying example:
state = BadInvariant()
state.initialize_1()
state.invariant_1()
state.teardown()
"""
    )


def test_invariant_present_in_falsifying_example():
    @Settings(print_blob=False)
    class BadRuleWithGoodInvariants(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.num = 0

        @initialize()
        def initialize_1(self):
            pass

        @invariant(check_during_init=True)
        def invariant_1(self):
            pass

        @invariant(check_during_init=False)
        def invariant_2(self):
            pass

        @precondition(lambda self: self.num > 0)
        @invariant()
        def invariant_3(self):
            pass

        @rule()
        def rule_1(self):
            self.num += 1
            if self.num == 2:
                raise ValueError()

    with capture_out() as o:
        with pytest.raises(ValueError):
            run_state_machine_as_test(BadRuleWithGoodInvariants)

    result = o.getvalue()
    assert (
        result
        == """\
Falsifying example:
state = BadRuleWithGoodInvariants()
state.invariant_1()
state.initialize_1()
state.invariant_1()
state.invariant_2()
state.rule_1()
state.invariant_1()
state.invariant_2()
state.invariant_3()
state.rule_1()
state.teardown()
"""
    )


def test_always_runs_at_least_one_step():
    class CountSteps(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.count = 0

        @rule()
        def do_something(self):
            self.count += 1

        def teardown(self):
            assert self.count > 0

    run_state_machine_as_test(CountSteps)


def test_removes_needless_steps():
    """Regression test from an example based on
    tests/nocover/test_database_agreement.py, but without the expensive bits.
    Comparing two database implementations in which deletion is broken, so as
    soon as a key/value pair is successfully deleted the test will now fail if
    you ever check that key.

    The main interesting feature of this is that it has a lot of
    opportunities to generate keys and values before it actually fails,
    but will still fail with very high probability.
    """

    @Settings(derandomize=True, max_examples=1000, deadline=None)
    class IncorrectDeletion(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.__saved = defaultdict(set)
            self.__deleted = defaultdict(set)

        keys = Bundle("keys")
        values = Bundle("values")

        @rule(target=keys, k=binary())
        def k(self, k):
            return k

        @rule(target=values, v=binary())
        def v(self, v):
            return v

        @rule(k=keys, v=values)
        def save(self, k, v):
            self.__saved[k].add(v)

        @rule(k=keys, v=values)
        def delete(self, k, v):
            if v in self.__saved[k]:
                self.__deleted[k].add(v)

        @rule(k=keys)
        def values_agree(self, k):
            assert not self.__deleted[k]

    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(IncorrectDeletion)

    assert o.getvalue().count(" = state.k(") == 1
    assert o.getvalue().count(" = state.v(") == 1


def test_prints_equal_values_with_correct_variable_name():
    @Settings(max_examples=100)
    class MovesBetweenBundles(RuleBasedStateMachine):
        b1 = Bundle("b1")
        b2 = Bundle("b2")

        @rule(target=b1)
        def create(self):
            return []

        @rule(target=b2, source=b1)
        def transfer(self, source):
            return source

        @rule(source=b2)
        def fail(self, source):
            raise AssertionError

    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(MovesBetweenBundles)

    result = o.getvalue()
    for m in ["create", "transfer", "fail"]:
        assert result.count("state." + m) == 1
    assert "v1 = state.create()" in result
    assert "v2 = state.transfer(source=v1)" in result
    assert "state.fail(source=v2)" in result


def test_initialize_rule():
    @Settings(max_examples=1000)
    class WithInitializeRules(RuleBasedStateMachine):
        initialized = []

        @initialize()
        def initialize_a(self):
            self.initialized.append("a")

        @initialize()
        def initialize_b(self):
            self.initialized.append("b")

        @initialize()
        def initialize_c(self):
            self.initialized.append("c")

        @rule()
        def fail_fast(self):
            raise AssertionError

    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(WithInitializeRules)

    assert set(WithInitializeRules.initialized[-3:]) == {"a", "b", "c"}
    result = o.getvalue().splitlines()[1:]
    assert result[0] == "state = WithInitializeRules()"
    # Initialize rules call order is shuffled
    assert {result[1], result[2], result[3]} == {
        "state.initialize_a()",
        "state.initialize_b()",
        "state.initialize_c()",
    }
    assert result[4] == "state.fail_fast()"
    assert result[5] == "state.teardown()"


def test_initialize_rule_populate_bundle():
    class WithInitializeBundleRules(RuleBasedStateMachine):
        a = Bundle("a")

        @initialize(target=a, dep=just("dep"))
        def initialize_a(self, dep):
            return f"a v1 with ({dep})"

        @rule(param=a)
        def fail_fast(self, param):
            raise AssertionError

    WithInitializeBundleRules.TestCase.settings = NO_BLOB_SETTINGS
    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(WithInitializeBundleRules)

    result = o.getvalue()
    assert (
        result
        == """\
Falsifying example:
state = WithInitializeBundleRules()
v1 = state.initialize_a(dep='dep')
state.fail_fast(param=v1)
state.teardown()
"""
    )


def test_initialize_rule_dont_mix_with_precondition():
    with pytest.raises(InvalidDefinition) as exc:

        class BadStateMachine(RuleBasedStateMachine):
            @precondition(lambda self: True)
            @initialize()
            def initialize(self):
                pass

    assert "An initialization rule cannot have a precondition." in str(exc.value)

    # Also test decorator application in reverse order

    with pytest.raises(InvalidDefinition) as exc:

        class BadStateMachineReverseOrder(RuleBasedStateMachine):
            @initialize()
            @precondition(lambda self: True)
            def initialize(self):
                pass

    assert "An initialization rule cannot have a precondition." in str(exc.value)


def test_initialize_rule_dont_mix_with_regular_rule():
    with pytest.raises(InvalidDefinition) as exc:

        class BadStateMachine(RuleBasedStateMachine):
            @rule()
            @initialize()
            def initialize(self):
                pass

    assert "A function cannot be used for two distinct rules." in str(exc.value)


def test_initialize_rule_cannot_be_double_applied():
    with pytest.raises(InvalidDefinition) as exc:

        class BadStateMachine(RuleBasedStateMachine):
            @initialize()
            @initialize()
            def initialize(self):
                pass

    assert "A function cannot be used for two distinct rules." in str(exc.value)


def test_initialize_rule_in_state_machine_with_inheritance():
    class ParentStateMachine(RuleBasedStateMachine):
        initialized = []

        @initialize()
        def initialize_a(self):
            self.initialized.append("a")

    class ChildStateMachine(ParentStateMachine):
        @initialize()
        def initialize_b(self):
            self.initialized.append("b")

        @rule()
        def fail_fast(self):
            raise AssertionError

    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(ChildStateMachine)

    assert set(ChildStateMachine.initialized[-2:]) == {"a", "b"}
    result = o.getvalue().splitlines()[1:]
    assert result[0] == "state = ChildStateMachine()"
    # Initialize rules call order is shuffled
    assert {result[1], result[2]} == {"state.initialize_a()", "state.initialize_b()"}
    assert result[3] == "state.fail_fast()"
    assert result[4] == "state.teardown()"


def test_can_manually_call_initialize_rule():
    class StateMachine(RuleBasedStateMachine):
        initialize_called_counter = 0

        @initialize()
        def initialize(self):
            self.initialize_called_counter += 1

        @rule()
        def fail_eventually(self):
            self.initialize()
            assert self.initialize_called_counter <= 2

    StateMachine.TestCase.settings = NO_BLOB_SETTINGS
    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(StateMachine)

    result = o.getvalue()
    assert (
        result
        == """\
Falsifying example:
state = StateMachine()
state.initialize()
state.fail_eventually()
state.fail_eventually()
state.teardown()
"""
    )


def test_steps_printed_despite_pytest_fail(capsys):
    # Test for https://github.com/HypothesisWorks/hypothesis/issues/1372
    @Settings(print_blob=False)
    class RaisesProblem(RuleBasedStateMachine):
        @rule()
        def oops(self):
            pytest.fail()

    with pytest.raises(Failed):
        run_state_machine_as_test(RaisesProblem)
    out, _ = capsys.readouterr()
    assert (
        """\
Falsifying example:
state = RaisesProblem()
state.oops()
state.teardown()
"""
        in out
    )


def test_steps_not_printed_with_pytest_skip(capsys):
    class RaisesProblem(RuleBasedStateMachine):
        @rule()
        def skip_whole_test(self):
            pytest.skip()

    with pytest.raises(Skipped):
        run_state_machine_as_test(RaisesProblem)
    out, _ = capsys.readouterr()
    assert "state" not in out


def test_rule_deprecation_targets_and_target():
    k, v = Bundle("k"), Bundle("v")
    with pytest.raises(InvalidArgument):
        rule(targets=(k,), target=v)


def test_rule_deprecation_bundle_by_name():
    Bundle("k")
    with pytest.raises(InvalidArgument):
        rule(target="k")


def test_rule_non_bundle_target():
    with pytest.raises(InvalidArgument):
        rule(target=integers())


def test_rule_non_bundle_target_oneof():
    k, v = Bundle("k"), Bundle("v")
    pattern = r".+ `one_of(a, b)` or `a | b` .+"
    with pytest.raises(InvalidArgument, match=pattern):
        rule(target=k | v)


def test_uses_seed(capsys):
    @seed(0)
    class TrivialMachine(RuleBasedStateMachine):
        @rule()
        def oops(self):
            raise AssertionError

    with pytest.raises(AssertionError):
        run_state_machine_as_test(TrivialMachine)
    out, _ = capsys.readouterr()
    assert "@seed" not in out


def test_reproduce_failure_works():
    @reproduce_failure(__version__, base64.b64encode(b"\x00\x00\x01\x00\x00\x00"))
    class TrivialMachine(RuleBasedStateMachine):
        @rule()
        def oops(self):
            raise AssertionError

    with pytest.raises(AssertionError):
        run_state_machine_as_test(TrivialMachine, settings=Settings(print_blob=True))


def test_reproduce_failure_fails_if_no_error():
    @reproduce_failure(__version__, base64.b64encode(b"\x00\x00\x01\x00\x00\x00"))
    class TrivialMachine(RuleBasedStateMachine):
        @rule()
        def ok(self):
            pass

    with pytest.raises(DidNotReproduce):
        run_state_machine_as_test(TrivialMachine, settings=Settings(print_blob=True))


def test_cannot_have_zero_steps():
    with pytest.raises(InvalidArgument):
        Settings(stateful_step_count=0)


def test_arguments_do_not_use_names_of_return_values():
    # See https://github.com/HypothesisWorks/hypothesis/issues/2341
    class TrickyPrintingMachine(RuleBasedStateMachine):
        data = Bundle("data")

        @initialize(target=data, value=integers())
        def init_data(self, value):
            return value

        @rule(d=data)
        def mostly_fails(self, d):
            assert d == 42

    with capture_out() as o:
        with pytest.raises(AssertionError):
            run_state_machine_as_test(TrickyPrintingMachine)
    output = o.getvalue()
    assert "v1 = state.init_data(value=0)" in output
    assert "v1 = state.init_data(value=v1)" not in output


def test_multiple_precondition_bug():
    # See https://github.com/HypothesisWorks/hypothesis/issues/2861
    class MultiplePreconditionMachine(RuleBasedStateMachine):
        @rule(x=integers())
        def good_method(self, x):
            pass

        @precondition(lambda self: True)
        @precondition(lambda self: False)
        @rule(x=integers())
        def bad_method_a(self, x):
            raise AssertionError("This rule runs, even though it shouldn't.")

        @precondition(lambda self: False)
        @precondition(lambda self: True)
        @rule(x=integers())
        def bad_method_b(self, x):
            raise AssertionError("This rule might be skipped for the wrong reason.")

        @precondition(lambda self: True)
        @rule(x=integers())
        @precondition(lambda self: False)
        def bad_method_c(self, x):
            raise AssertionError("This rule runs, even though it shouldn't.")

        @rule(x=integers())
        @precondition(lambda self: True)
        @precondition(lambda self: False)
        def bad_method_d(self, x):
            raise AssertionError("This rule runs, even though it shouldn't.")

        @precondition(lambda self: True)
        @precondition(lambda self: False)
        @invariant()
        def bad_invariant_a(self):
            raise AssertionError("This invariant runs, even though it shouldn't.")

        @precondition(lambda self: False)
        @precondition(lambda self: True)
        @invariant()
        def bad_invariant_b(self):
            raise AssertionError("This invariant runs, even though it shouldn't.")

        @precondition(lambda self: True)
        @invariant()
        @precondition(lambda self: False)
        def bad_invariant_c(self):
            raise AssertionError("This invariant runs, even though it shouldn't.")

        @invariant()
        @precondition(lambda self: True)
        @precondition(lambda self: False)
        def bad_invariant_d(self):
            raise AssertionError("This invariant runs, even though it shouldn't.")

    run_state_machine_as_test(MultiplePreconditionMachine)


class TrickyInitMachine(RuleBasedStateMachine):
    @initialize()
    def init_a(self):
        self.a = 0

    @rule()
    def inc(self):
        self.a += 1

    @invariant()
    def check_a_positive(self):
        # This will fail if run before the init_a method, but without
        # @invariant(check_during_init=True) it will only run afterwards.
        assert self.a >= 0


def test_invariants_are_checked_after_init_steps():
    run_state_machine_as_test(TrickyInitMachine)


def test_invariants_can_be_checked_during_init_steps():
    class UndefinedMachine(TrickyInitMachine):
        @invariant(check_during_init=True)
        def check_a_defined(self):
            # This will fail because `a` is undefined before the init rule.
            self.a

    with pytest.raises(AttributeError):
        run_state_machine_as_test(UndefinedMachine)


def test_check_during_init_must_be_boolean():
    invariant(check_during_init=False)
    invariant(check_during_init=True)
    with pytest.raises(InvalidArgument):
        invariant(check_during_init="not a bool")


def test_deprecated_target_consumes_bundle():
    # It would be nicer to raise this error at runtime, but the internals make
    # this sadly impractical.  Most InvalidDefinition errors happen at, well,
    # definition-time already anyway, so it's not *worse* than the status quo.
    with validate_deprecation():
        rule(target=consumes(Bundle("b")))
