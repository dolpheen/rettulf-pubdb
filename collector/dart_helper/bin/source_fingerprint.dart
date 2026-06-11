import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/visitor.dart';
import 'package:pub_semver/pub_semver.dart';

final _parserFeatureSet = FeatureSet.fromEnableFlags2(
  sdkLanguageVersion: Version(3, 12, 0),
  flags: const <String>[],
);

Future<void> main(List<String> args) async {
  try {
    final options = _Options.parse(args);
    final fingerprint = _Collector(options.packageDir).collect();
    stdout.writeln(jsonEncode(fingerprint.toJson()));
  } catch (error) {
    stderr.writeln(error);
    exitCode = 1;
  }
}

class _Collector {
  _Collector(this.packageDir);

  final Directory packageDir;
  late final Directory libDir = Directory(_join([packageDir.path, 'lib']));
  final Map<String, CompilationUnit> _units = <String, CompilationUnit>{};

  _SourceFingerprint collect() {
    if (!libDir.existsSync()) {
      return _SourceFingerprint();
    }

    for (final file in _dartFiles()) {
      _unitFor(_relativePath(file));
    }

    final constCollector = _ConstStringCollector();
    for (final unit in _orderedUnits()) {
      unit.accept(constCollector);
    }

    final visitor = _SourceFingerprintVisitor(constCollector.strings);
    for (final unit in _orderedUnits()) {
      unit.accept(visitor);
    }
    return visitor.fingerprint;
  }

  List<File> _dartFiles() {
    return libDir
        .listSync(recursive: true)
        .whereType<File>()
        .where((file) => file.path.endsWith('.dart') && !_isAppleDouble(file))
        .toList()
      ..sort((a, b) => _relativePath(a).compareTo(_relativePath(b)));
  }

  CompilationUnit _unitFor(String relativePath) {
    final normalized = _normalizePath(relativePath);
    return _units.putIfAbsent(normalized, () {
      final file = File(_join([libDir.path, normalized]));
      final result = parseString(
        content: file.readAsStringSync(),
        path: file.path,
        featureSet: _parserFeatureSet,
        throwIfDiagnostics: false,
      );
      return result.unit;
    });
  }

  List<CompilationUnit> _orderedUnits() {
    return (_units.entries.toList()..sort((a, b) => a.key.compareTo(b.key)))
        .map((entry) => entry.value)
        .toList();
  }

  String _relativePath(File file) {
    return _normalizePath(file.path.substring(libDir.path.length + 1));
  }
}

class _ConstStringCollector extends RecursiveAstVisitor<void> {
  final Map<String, Set<String>> strings = <String, Set<String>>{};
  String? _currentClass;

  @override
  void visitClassDeclaration(ClassDeclaration node) {
    final previousClass = _currentClass;
    _currentClass = node.namePart.typeName.lexeme;
    super.visitClassDeclaration(node);
    _currentClass = previousClass;
  }

  @override
  void visitTopLevelVariableDeclaration(TopLevelVariableDeclaration node) {
    if (node.variables.isConst) {
      _collectVariables(node.variables);
    }
    super.visitTopLevelVariableDeclaration(node);
  }

  @override
  void visitFieldDeclaration(FieldDeclaration node) {
    if (node.fields.isConst) {
      _collectVariables(node.fields);
    }
    super.visitFieldDeclaration(node);
  }

  void _collectVariables(VariableDeclarationList variables) {
    for (final variable in variables.variables) {
      final value = _literalString(variable.initializer);
      if (value == null) {
        continue;
      }
      _add(variable.name.lexeme, value);
      final className = _currentClass;
      if (className != null) {
        _add('$className.${variable.name.lexeme}', value);
      }
    }
  }

  void _add(String name, String value) {
    strings.putIfAbsent(name, () => <String>{}).add(value);
  }
}

class _SourceFingerprintVisitor extends RecursiveAstVisitor<void> {
  _SourceFingerprintVisitor(this._constStrings);

  final Map<String, Set<String>> _constStrings;
  final _SourceFingerprint fingerprint = _SourceFingerprint();

  @override
  void visitClassDeclaration(ClassDeclaration node) {
    final name = node.namePart.typeName.lexeme;
    fingerprint.hierarchy.add(
      _HierarchyEntry(
        className: name,
        superName: _typeName(node.extendsClause?.superclass) ?? '',
        fieldsCount: _fieldCount(node),
        methodsCount: _methodCount(node),
      ),
    );
    if (_hasConstConstructor(node)) {
      fingerprint.constClasses.add(name);
    }
    super.visitClassDeclaration(node);
  }

  @override
  void visitAdjacentStrings(AdjacentStrings node) {
    _addStringLiteral(node);
  }

  @override
  void visitSimpleStringLiteral(SimpleStringLiteral node) {
    _addStringLiteral(node);
  }

  @override
  void visitStringInterpolation(StringInterpolation node) {
    _addStringLiteral(node);
    super.visitStringInterpolation(node);
  }

  @override
  void visitInstanceCreationExpression(InstanceCreationExpression node) {
    final channelName = _literalOrConstString(
      _firstArgument(node.argumentList),
    );
    if (channelName != null) {
      _addChannel(node.constructorName.type.name.lexeme, channelName);
    }
    super.visitInstanceCreationExpression(node);
  }

  @override
  void visitAnnotation(Annotation node) {
    if (_lastName(node.name.name) == 'Native') {
      final symbol = _firstStringArgument(node.arguments);
      if (symbol != null) {
        fingerprint.ffiSymbols.add(symbol);
      }
    }
    super.visitAnnotation(node);
  }

  @override
  void visitMethodInvocation(MethodInvocation node) {
    if (node.methodName.name == 'lookupFunction') {
      final symbol = _firstStringArgument(node.argumentList);
      if (symbol != null) {
        fingerprint.ffiSymbols.add(symbol);
      }
    } else if (_isChannelType(node.methodName.name)) {
      final channelName = _firstStringArgument(node.argumentList);
      if (channelName != null) {
        _addChannel(node.methodName.name, channelName);
      }
    }
    super.visitMethodInvocation(node);
  }

  void _addStringLiteral(StringLiteral node) {
    final value = node.stringValue;
    if (value != null) {
      fingerprint.stringLiterals.add(value);
    }
  }

  String? _literalOrConstString(Expression? expression) {
    final literal = _literalString(expression);
    if (literal != null) {
      return literal;
    }
    final name = _constName(expression);
    if (name == null) {
      return null;
    }
    final values = _constStrings[name];
    if (values == null || values.length != 1) {
      return null;
    }
    return values.single;
  }

  void _addChannel(String constructorType, String channelName) {
    if (constructorType == 'MethodChannel') {
      fingerprint.methodChannels.add(channelName);
    } else if (constructorType == 'EventChannel') {
      fingerprint.eventChannels.add(channelName);
    } else if (constructorType == 'BasicMessageChannel') {
      fingerprint.basicMessageChannels.add(channelName);
    }
  }

  String? _firstStringArgument(ArgumentList? arguments) {
    if (arguments == null) {
      return null;
    }
    for (final argument in arguments.arguments) {
      if (argument is Expression) {
        final value = _literalOrConstString(argument);
        if (value != null) {
          return value;
        }
      }
    }
    for (final argument in arguments.arguments.whereType<NamedArgument>()) {
      if (argument.name.lexeme != 'symbol') {
        continue;
      }
      final value = _literalOrConstString(argument.argumentExpression);
      if (value != null) {
        return value;
      }
    }
    return null;
  }
}

bool _isChannelType(String name) {
  return const {
    'MethodChannel',
    'EventChannel',
    'BasicMessageChannel',
  }.contains(name);
}

class _SourceFingerprint {
  final List<_HierarchyEntry> hierarchy = <_HierarchyEntry>[];
  final Set<String> stringLiterals = <String>{};
  final Set<String> methodChannels = <String>{};
  final Set<String> eventChannels = <String>{};
  final Set<String> basicMessageChannels = <String>{};
  final Set<String> ffiSymbols = <String>{};
  final Set<String> constClasses = <String>{};

  Map<String, Object?> toJson() {
    final sortedHierarchy = hierarchy.toList()..sort(_compareHierarchyEntry);
    return {
      '_hierarchy': sortedHierarchy.map((entry) => entry.toJson()).toList(),
      'string_literals': _sorted(stringLiterals),
      'method_channels': _sorted(methodChannels),
      'event_channels': _sorted(eventChannels),
      'basic_message_channels': _sorted(basicMessageChannels),
      'ffi_symbols': _sorted(ffiSymbols),
      'const_classes': _sorted(constClasses),
    };
  }
}

class _HierarchyEntry {
  _HierarchyEntry({
    required this.className,
    required this.superName,
    required this.fieldsCount,
    required this.methodsCount,
  });

  final String className;
  final String superName;
  final int fieldsCount;
  final int methodsCount;

  Map<String, Object?> toJson() {
    return {
      'class': className,
      'super': superName,
      'fields_count': fieldsCount,
      'methods_count': methodsCount,
    };
  }
}

class _Options {
  _Options({required this.packageDir});

  final Directory packageDir;

  static _Options parse(List<String> args) {
    final packageDir = _option(args, '--package-dir');
    if (packageDir == null || packageDir.isEmpty) {
      throw FormatException('--package-dir is required');
    }
    return _Options(packageDir: Directory(packageDir));
  }

  static String? _option(List<String> args, String name) {
    final index = args.indexOf(name);
    if (index < 0 || index + 1 >= args.length) {
      return null;
    }
    return args[index + 1];
  }
}

int _fieldCount(ClassDeclaration declaration) {
  final namePart = declaration.namePart;
  var count = namePart is PrimaryConstructorDeclaration
      ? _fieldFormalCount(namePart.formalParameters)
      : 0;
  final body = declaration.body;
  if (body is! BlockClassBody) {
    return count;
  }
  for (final member in body.members.whereType<FieldDeclaration>()) {
    count += member.fields.variables.length;
  }
  return count;
}

int _fieldFormalCount(FormalParameterList parameters) {
  return parameters.parameters.where(_isPrimaryConstructorField).length;
}

bool _isPrimaryConstructorField(FormalParameter parameter) {
  return parameter is FieldFormalParameter ||
      parameter.constFinalOrVarKeyword != null;
}

int _methodCount(ClassDeclaration declaration) {
  var count = declaration.namePart is PrimaryConstructorDeclaration ? 1 : 0;
  final body = declaration.body;
  if (body is! BlockClassBody) {
    return count;
  }
  for (final member in body.members) {
    if (member is ConstructorDeclaration || member is MethodDeclaration) {
      count += 1;
    }
  }
  return count;
}

bool _hasConstConstructor(ClassDeclaration declaration) {
  final namePart = declaration.namePart;
  if (namePart is PrimaryConstructorDeclaration &&
      namePart.constKeyword != null) {
    return true;
  }
  final body = declaration.body;
  if (body is! BlockClassBody) {
    return false;
  }
  return body.members.whereType<ConstructorDeclaration>().any(
    (member) => member.constKeyword != null,
  );
}

Expression? _firstArgument(ArgumentList arguments) {
  for (final argument in arguments.arguments) {
    if (argument is Expression) {
      return argument;
    }
  }
  return null;
}

String? _literalString(Expression? expression) {
  if (expression is StringLiteral) {
    return expression.stringValue;
  }
  if (expression is ParenthesizedExpression) {
    return _literalString(expression.expression);
  }
  return null;
}

String? _constName(Expression? expression) {
  if (expression is SimpleIdentifier) {
    return expression.name;
  }
  if (expression is PrefixedIdentifier) {
    return expression.name;
  }
  if (expression is PropertyAccess && expression.target != null) {
    return '${expression.target!.toSource()}.${expression.propertyName.name}';
  }
  if (expression is ParenthesizedExpression) {
    return _constName(expression.expression);
  }
  return null;
}

String? _typeName(NamedType? type) {
  return type?.name.lexeme;
}

String _lastName(String name) {
  final parts = name.split('.');
  return parts.isEmpty ? name : parts.last;
}

bool _isAppleDouble(File file) =>
    file.uri.pathSegments.last.startsWith('._');

String _normalizePath(String path) =>
    path.replaceAll(Platform.pathSeparator, '/');

String _join(List<String> parts) => parts.join(Platform.pathSeparator);

List<String> _sorted(Set<String> values) => values.toList()..sort();

int _compareHierarchyEntry(_HierarchyEntry a, _HierarchyEntry b) {
  final classComparison = a.className.compareTo(b.className);
  if (classComparison != 0) {
    return classComparison;
  }
  final superComparison = a.superName.compareTo(b.superName);
  if (superComparison != 0) {
    return superComparison;
  }
  final fieldComparison = a.fieldsCount.compareTo(b.fieldsCount);
  if (fieldComparison != 0) {
    return fieldComparison;
  }
  return a.methodsCount.compareTo(b.methodsCount);
}
