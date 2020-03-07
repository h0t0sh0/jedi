"""
PEP 0484 ( https://www.python.org/dev/peps/pep-0484/ ) describes type hints
through function annotations. There is a strong suggestion in this document
that only the type of type hinting defined in PEP0484 should be allowed
as annotations in future python versions.
"""

import re

from parso import ParserSyntaxError, parse

from jedi._compatibility import force_unicode, Parameter
from jedi.inference.cache import inference_state_method_cache
from jedi.inference.base_value import ValueSet, NO_VALUES
from jedi.inference.gradual.base import DefineGenericBase, GenericClass
from jedi.inference.gradual.generics import TupleGenericManager
from jedi.inference.gradual.typing import TypingClassValueWithIndex
from jedi.inference.gradual.type_var import TypeVar
from jedi.inference.helpers import is_string
from jedi.inference.compiled import builtin_from_name
from jedi.inference.param import get_executed_param_names
from jedi import debug
from jedi import parser_utils


def infer_annotation(context, annotation):
    """
    Inferes an annotation node. This means that it inferes the part of
    `int` here:

        foo: int = 3

    Also checks for forward references (strings)
    """
    value_set = context.infer_node(annotation)
    if len(value_set) != 1:
        debug.warning("Inferred typing index %s should lead to 1 object, "
                      " not %s" % (annotation, value_set))
        return value_set

    inferred_value = list(value_set)[0]
    if is_string(inferred_value):
        result = _get_forward_reference_node(context, inferred_value.get_safe_value())
        if result is not None:
            return context.infer_node(result)
    return value_set


def _infer_annotation_string(context, string, index=None):
    node = _get_forward_reference_node(context, string)
    if node is None:
        return NO_VALUES

    value_set = context.infer_node(node)
    if index is not None:
        value_set = value_set.filter(
            lambda value: value.array_type == u'tuple'  # noqa
                            and len(list(value.py__iter__())) >= index
        ).py__simple_getitem__(index)
    return value_set


def _get_forward_reference_node(context, string):
    try:
        new_node = context.inference_state.grammar.parse(
            force_unicode(string),
            start_symbol='eval_input',
            error_recovery=False
        )
    except ParserSyntaxError:
        debug.warning('Annotation not parsed: %s' % string)
        return None
    else:
        module = context.tree_node.get_root_node()
        parser_utils.move(new_node, module.end_pos[0])
        new_node.parent = context.tree_node
        return new_node


def _split_comment_param_declaration(decl_text):
    """
    Split decl_text on commas, but group generic expressions
    together.

    For example, given "foo, Bar[baz, biz]" we return
    ['foo', 'Bar[baz, biz]'].

    """
    try:
        node = parse(decl_text, error_recovery=False).children[0]
    except ParserSyntaxError:
        debug.warning('Comment annotation is not valid Python: %s' % decl_text)
        return []

    if node.type in ['name', 'atom_expr', 'power']:
        return [node.get_code().strip()]

    params = []
    try:
        children = node.children
    except AttributeError:
        return []
    else:
        for child in children:
            if child.type in ['name', 'atom_expr', 'power']:
                params.append(child.get_code().strip())

    return params


@inference_state_method_cache()
def infer_param(function_value, param, ignore_stars=False):
    values = _infer_param(function_value, param)
    if ignore_stars:
        return values
    inference_state = function_value.inference_state
    if param.star_count == 1:
        tuple_ = builtin_from_name(inference_state, 'tuple')
        return ValueSet([GenericClass(
            tuple_,
            TupleGenericManager((values,)),
        ) for c in values])
    elif param.star_count == 2:
        dct = builtin_from_name(inference_state, 'dict')
        generics = (
            ValueSet([builtin_from_name(inference_state, 'str')]),
            values
        )
        return ValueSet([GenericClass(
            dct,
            TupleGenericManager(generics),
        ) for c in values])
        pass
    return values


def _infer_param(function_value, param):
    """
    Infers the type of a function parameter, using type annotations.
    """
    annotation = param.annotation
    if annotation is None:
        # If no Python 3-style annotation, look for a Python 2-style comment
        # annotation.
        # Identify parameters to function in the same sequence as they would
        # appear in a type comment.
        all_params = [child for child in param.parent.children
                      if child.type == 'param']

        node = param.parent.parent
        comment = parser_utils.get_following_comment_same_line(node)
        if comment is None:
            return NO_VALUES

        match = re.match(r"^#\s*type:\s*\(([^#]*)\)\s*->", comment)
        if not match:
            return NO_VALUES
        params_comments = _split_comment_param_declaration(match.group(1))

        # Find the specific param being investigated
        index = all_params.index(param)
        # If the number of parameters doesn't match length of type comment,
        # ignore first parameter (assume it's self).
        if len(params_comments) != len(all_params):
            debug.warning(
                "Comments length != Params length %s %s",
                params_comments, all_params
            )
        if function_value.is_bound_method():
            if index == 0:
                # Assume it's self, which is already handled
                return NO_VALUES
            index -= 1
        if index >= len(params_comments):
            return NO_VALUES

        param_comment = params_comments[index]
        return _infer_annotation_string(
            function_value.get_default_param_context(),
            param_comment
        )
    # Annotations are like default params and resolve in the same way.
    context = function_value.get_default_param_context()
    return infer_annotation(context, annotation)


def py__annotations__(funcdef):
    dct = {}
    for function_param in funcdef.get_params():
        param_annotation = function_param.annotation
        if param_annotation is not None:
            dct[function_param.name.value] = param_annotation

    return_annotation = funcdef.annotation
    if return_annotation:
        dct['return'] = return_annotation
    return dct


@inference_state_method_cache()
def infer_return_types(function, arguments):
    """
    Infers the type of a function's return value,
    according to type annotations.
    """
    all_annotations = py__annotations__(function.tree_node)
    annotation = all_annotations.get("return", None)
    if annotation is None:
        # If there is no Python 3-type annotation, look for a Python 2-type annotation
        node = function.tree_node
        comment = parser_utils.get_following_comment_same_line(node)
        if comment is None:
            return NO_VALUES

        match = re.match(r"^#\s*type:\s*\([^#]*\)\s*->\s*([^#]*)", comment)
        if not match:
            return NO_VALUES

        return _infer_annotation_string(
            function.get_default_param_context(),
            match.group(1).strip()
        ).execute_annotation()
        if annotation is None:
            return NO_VALUES

    context = function.get_default_param_context()
    unknown_type_vars = find_unknown_type_vars(context, annotation)
    annotation_values = infer_annotation(context, annotation)
    if not unknown_type_vars:
        return annotation_values.execute_annotation()

    type_var_dict = infer_type_vars_for_execution(function, arguments, all_annotations)

    return ValueSet.from_sets(
        ann.define_generics(type_var_dict)
        if isinstance(ann, (DefineGenericBase, TypeVar)) else ValueSet({ann})
        for ann in annotation_values
    ).execute_annotation()


def infer_type_vars_for_execution(function, arguments, annotation_dict):
    """
    Some functions use type vars that are not defined by the class, but rather
    only defined in the function. See for example `iter`. In those cases we
    want to:

    1. Search for undefined type vars.
    2. Infer type vars with the execution state we have.
    3. Return the union of all type vars that have been found.
    """
    context = function.get_default_param_context()

    annotation_variable_results = {}
    executed_param_names = get_executed_param_names(function, arguments)
    for executed_param_name in executed_param_names:
        try:
            annotation_node = annotation_dict[executed_param_name.string_name]
        except KeyError:
            continue

        annotation_variables = find_unknown_type_vars(context, annotation_node)
        if annotation_variables:
            # Infer unknown type var
            annotation_value_set = context.infer_node(annotation_node)
            kind = executed_param_name.get_kind()
            actual_value_set = executed_param_name.infer()
            if kind is Parameter.VAR_POSITIONAL:
                actual_value_set = actual_value_set.merge_types_of_iterate()
            elif kind is Parameter.VAR_KEYWORD:
                # TODO _dict_values is not public.
                actual_value_set = actual_value_set.try_merge('_dict_values')
            for ann in annotation_value_set:
                _merge_type_var_dicts(
                    annotation_variable_results,
                    _infer_type_vars(ann, actual_value_set),
                )
    return annotation_variable_results


def infer_return_for_callable(arguments, param_values, result_values):
    all_type_vars = {}
    for pv in param_values:
        if pv.array_type == 'list':
            type_var_dict = infer_type_vars_for_callable(arguments, pv.py__iter__())
            all_type_vars.update(type_var_dict)

    return ValueSet.from_sets(
        v.define_generics(all_type_vars)
        if isinstance(v, (DefineGenericBase, TypeVar)) else ValueSet({v})
        for v in result_values
    ).execute_annotation()


def infer_type_vars_for_callable(arguments, lazy_params):
    """
    Infers type vars for the Calllable class:

        def x() -> Callable[[Callable[..., _T]], _T]: ...
    """
    annotation_variable_results = {}
    for (_, lazy_value), lazy_callable_param in zip(arguments.unpack(), lazy_params):
        callable_param_values = lazy_callable_param.infer()
        # Infer unknown type var
        actual_value_set = lazy_value.infer()
        for v in callable_param_values:
            _merge_type_var_dicts(
                annotation_variable_results,
                _infer_type_vars(v, actual_value_set),
            )
    return annotation_variable_results


def _merge_type_var_dicts(base_dict, new_dict):
    for type_var_name, values in new_dict.items():
        if values:
            try:
                base_dict[type_var_name] |= values
            except KeyError:
                base_dict[type_var_name] = values


def _infer_type_vars(annotation_value, value_set, is_class_value=False):
    """
    This function tries to find information about undefined type vars and
    returns a dict from type var name to value set.

    This is for example important to understand what `iter([1])` returns.
    According to typeshed, `iter` returns an `Iterator[_T]`:

        def iter(iterable: Iterable[_T]) -> Iterator[_T]: ...

    This functions would generate `int` for `_T` in this case, because it
    unpacks the `Iterable`.

    Parameters
    ----------

    `annotation_value`: represents the annotation of the current parameter to
        infer the value for. In the above example, this would initially be the
        `Iterable[_T]` of the `iterable` parameter and then, when recursing,
        just the `_T` generic parameter.

    `value_set`: represents the actual argument passed to the parameter we're
        inferrined for, or (for recursive calls) their types. In the above
        example this would first be the representation of the list `[1]` and
        then, when recursing, just of `1`.

    `is_class_value`: tells us whether or not to treat the `value_set` as
        representing the instances or types being passed, which is neccesary to
        correctly cope with `Type[T]` annotations. When it is True, this means
        that we are being called with a nested portion of an annotation and that
        the `value_set` represents the types of the arguments, rather than their
        actual instances.
        Note: not all recursive calls will neccesarily set this to True.
    """
    type_var_dict = {}
    annotation_name = annotation_value.py__name__()

    if isinstance(annotation_value, TypeVar):
        if not is_class_value:
            return {annotation_name: value_set.py__class__()}
        return {annotation_name: value_set}
    elif isinstance(annotation_value, TypingClassValueWithIndex):
        if annotation_name == 'Type':
            given = annotation_value.get_generics()
            if given:
                if is_class_value:
                    for element in value_set:
                        element_name = element.py__name__()
                        if annotation_name == element_name:
                            annotation_generics = annotation_value.get_generics()
                            actual_generics = element.get_generics()

                            for annotation_generics_set, actual_generic_set in zip(annotation_generics, actual_generics):
                                for nested_annotation_value in annotation_generics_set:
                                    _merge_type_var_dicts(
                                        type_var_dict,
                                        _infer_type_vars(
                                            nested_annotation_value,
                                            actual_generic_set,
                                            # This is a note to ourselves that we
                                            # have already converted the instance
                                            # representation to its class.
                                            is_class_value=True,
                                        ),
                                    )
                else:
                    for nested_annotation_value in given[0]:
                        _merge_type_var_dicts(
                            type_var_dict,
                            _infer_type_vars(
                                nested_annotation_value,
                                value_set,
                                is_class_value=True,
                            )
                        )
        elif annotation_name == 'Callable':
            given = annotation_value.get_generics()
            if len(given) == 2:
                for nested_annotation_value in given[1]:
                    _merge_type_var_dicts(
                        type_var_dict,
                        _infer_type_vars(
                            nested_annotation_value,
                            value_set.execute_annotation(),
                        )
                    )
        elif annotation_name == 'Tuple':
            # TODO: this logic is pretty similar to the general logic below, can
            # we combine them?

            for element in value_set:
                py_class = element.get_annotated_class_object()
                if not isinstance(py_class, GenericClass):
                    py_class = element

                if not isinstance(py_class, DefineGenericBase):
                    continue

                annotation_generics = annotation_value.get_generics()
                tuple_annotation, = annotation_value.execute_annotation()
                # TODO: is can we avoid using this private method?
                if tuple_annotation._is_homogenous():
                    for nested_annotation_value in annotation_generics[0]:
                        _merge_type_var_dicts(
                            type_var_dict,
                            _infer_type_vars(
                                nested_annotation_value,
                                value_set.merge_types_of_iterate(),
                            ),
                        )
                else:
                    actual_generics = py_class.get_generics()
                    for annotation_generics_set, actual_generic_set in zip(annotation_generics, actual_generics):
                        for nested_annotation_value in annotation_generics_set:
                            _merge_type_var_dicts(
                                type_var_dict,
                                _infer_type_vars(
                                    nested_annotation_value,
                                    actual_generic_set,
                                    # This is a note to ourselves that we
                                    # have already converted the instance
                                    # representation to its class.
                                    is_class_value=True,
                                ),
                            )
    elif isinstance(annotation_value, GenericClass):
        if annotation_name == 'Iterable' and not is_class_value:
            given = annotation_value.get_generics()
            if given:
                for nested_annotation_value in given[0]:
                    _merge_type_var_dicts(
                        type_var_dict,
                        _infer_type_vars(
                            nested_annotation_value,
                            value_set.merge_types_of_iterate(),
                        )
                    )
        else:
            # Note: we need to handle the MRO _in order_, so we need to extract
            # the elements from the set first, then handle them, even if we put
            # them back in a set afterwards.
            for element in value_set:
                if not hasattr(element, 'is_instance'):
                    continue

                if element.is_instance():
                    py_class = element.get_annotated_class_object()
                else:
                    py_class = element

                for parent_class in py_class.py__mro__():
                    class_name = parent_class.py__name__()
                    if annotation_name == class_name:
                        if not isinstance(parent_class, DefineGenericBase):
                            continue

                        annotation_generics = annotation_value.get_generics()
                        actual_generics = parent_class.get_generics()

                        for annotation_generics_set, actual_generic_set in zip(annotation_generics, actual_generics):
                            for nested_annotation_value in annotation_generics_set:
                                _merge_type_var_dicts(
                                    type_var_dict,
                                    _infer_type_vars(
                                        nested_annotation_value,
                                        actual_generic_set,
                                        # This is a note to ourselves that we
                                        # have already converted the instance
                                        # representation to its class.
                                        is_class_value=True,
                                    ),
                                )

                        break

    return type_var_dict


def find_type_from_comment_hint_for(context, node, name):
    return _find_type_from_comment_hint(context, node, node.children[1], name)


def find_type_from_comment_hint_with(context, node, name):
    assert len(node.children[1].children) == 3, \
        "Can only be here when children[1] is 'foo() as f'"
    varlist = node.children[1].children[2]
    return _find_type_from_comment_hint(context, node, varlist, name)


def find_type_from_comment_hint_assign(context, node, name):
    return _find_type_from_comment_hint(context, node, node.children[0], name)


def _find_type_from_comment_hint(context, node, varlist, name):
    index = None
    if varlist.type in ("testlist_star_expr", "exprlist", "testlist"):
        # something like "a, b = 1, 2"
        index = 0
        for child in varlist.children:
            if child == name:
                break
            if child.type == "operator":
                continue
            index += 1
        else:
            return []

    comment = parser_utils.get_following_comment_same_line(node)
    if comment is None:
        return []
    match = re.match(r"^#\s*type:\s*([^#]*)", comment)
    if match is None:
        return []
    return _infer_annotation_string(
        context, match.group(1).strip(), index
    ).execute_annotation()


def find_unknown_type_vars(context, node):
    def check_node(node):
        if node.type in ('atom_expr', 'power'):
            trailer = node.children[-1]
            if trailer.type == 'trailer' and trailer.children[0] == '[':
                for subscript_node in _unpack_subscriptlist(trailer.children[1]):
                    check_node(subscript_node)
        else:
            found[:] = _filter_type_vars(context.infer_node(node), found)

    found = []  # We're not using a set, because the order matters.
    check_node(node)
    return found


def _filter_type_vars(value_set, found=()):
    new_found = list(found)
    for type_var in value_set:
        if isinstance(type_var, TypeVar) and type_var not in found:
            new_found.append(type_var)
    return new_found


def _unpack_subscriptlist(subscriptlist):
    if subscriptlist.type == 'subscriptlist':
        for subscript in subscriptlist.children[::2]:
            if subscript.type != 'subscript':
                yield subscript
    else:
        if subscriptlist.type != 'subscript':
            yield subscriptlist
