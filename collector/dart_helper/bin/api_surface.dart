import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/visitor.dart';

Future<void> main(List<String> args) async {
  try {
    final options = _Options.parse(args);
    final surface = _Collector(options.packageDir, options.package).collect();
    stdout.writeln(jsonEncode(surface.toJson()));
  } catch (error) {
    stderr.writeln(error);
    exitCode = 1;
  }
}

class _Collector {
  _Collector(this.packageDir, this.package);

  final Directory packageDir;
  final String package;
  late final Directory libDir = Directory(_join([packageDir.path, 'lib']));
  final Map<String, CompilationUnit> _units = <String, CompilationUnit>{};
  final Map<String, _ClassSurface> _classes = <String, _ClassSurface>{};

  _ApiSurface collect() {
    if (!libDir.existsSync()) {
      return _ApiSurface(_classes);
    }

    for (final file in _dartFiles()) {
      _unitFor(_relativePath(file));
    }

    for (final entry in _units.entries.toList()..sort(_compareUnitEntry)) {
      if (_isPartFile(entry.value)) {
        continue;
      }
      final libraryUri = _libraryUri(entry.key);
      _collectLibrary(entry.key, libraryUri, <String>{});
      _collectExports(entry.key, libraryUri, <String>{});
    }

    return _ApiSurface(_classes);
  }

  List<File> _dartFiles() {
    return libDir
        .listSync(recursive: true)
        .whereType<File>()
        .where((file) => file.path.endsWith('.dart'))
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
        featureSet: FeatureSet.latestLanguageVersion(),
        throwIfDiagnostics: false,
      );
      return result.unit;
    });
  }

  void _collectLibrary(
    String relativePath,
    String libraryUri,
    Set<String> visiting,
  ) {
    final normalized = _normalizePath(relativePath);
    if (!visiting.add(normalized)) {
      return;
    }

    final unit = _unitFor(normalized);
    _collectUnitDeclarations(unit, libraryUri);
    for (final partPath in _partPaths(unit, normalized)) {
      _collectLibrary(partPath, libraryUri, visiting);
    }
    visiting.remove(normalized);
  }

  void _collectExports(
    String relativePath,
    String libraryUri,
    Set<String> visiting,
  ) {
    final normalized = _normalizePath(relativePath);
    if (!visiting.add(normalized)) {
      return;
    }

    final unit = _unitFor(normalized);
    for (final directive in unit.directives.whereType<ExportDirective>()) {
      final exportPath = _localUriPath(directive.uri.stringValue, normalized);
      if (exportPath == null) {
        _surface(
          '::',
          libraryUri,
        ).types.add('export:${directive.uri.stringValue ?? ''}');
        continue;
      }

      final exported = _collectStandaloneSurface(exportPath);
      final showNames = <String>{};
      final hideNames = <String>{};
      for (final combinator in directive.combinators) {
        if (combinator is ShowCombinator) {
          showNames.addAll(
            combinator.shownNames.map((identifier) => identifier.name),
          );
        } else if (combinator is HideCombinator) {
          hideNames.addAll(
            combinator.hiddenNames.map((identifier) => identifier.name),
          );
        }
      }
      exported.reexportAs(
        libraryUri,
        showNames: showNames,
        hideNames: hideNames,
      );
      _merge(exported.classes);
      _collectExports(exportPath, libraryUri, visiting);
    }
    visiting.remove(normalized);
  }

  _ApiSurface _collectStandaloneSurface(String relativePath) {
    final saved = <String, _ClassSurface>{};
    final before = Map<String, _ClassSurface>.from(_classes);
    try {
      _classes.clear();
      _collectLibrary(relativePath, _libraryUri(relativePath), <String>{});
      saved.addAll(_classes);
    } finally {
      _classes
        ..clear()
        ..addAll(before);
    }
    return _ApiSurface(saved);
  }

  void _collectUnitDeclarations(CompilationUnit unit, String libraryUri) {
    final topLevel = _ClassSurface();
    for (final declaration in unit.declarations) {
      if (declaration is ClassDeclaration) {
        _collectClass(declaration, libraryUri);
      } else if (declaration is MixinDeclaration) {
        _collectMixin(declaration, libraryUri);
      } else if (declaration is EnumDeclaration) {
        _collectEnum(declaration, libraryUri);
      } else if (declaration is ExtensionDeclaration) {
        _collectExtension(declaration, libraryUri);
      } else if (declaration is FunctionDeclaration) {
        final name = _publicName(declaration.name.lexeme);
        if (name == null) {
          continue;
        }
        if (declaration.isGetter) {
          topLevel.fields.add('get:$name');
        } else if (declaration.isSetter) {
          topLevel.fields.add('set:$name');
          topLevel.methods.add(
            _callableSignature(
              'setter',
              name,
              declaration.functionExpression.parameters,
            ),
          );
        } else {
          topLevel.methods.add(name);
          topLevel.methods.add(
            _callableSignature(
              'function',
              name,
              declaration.functionExpression.parameters,
            ),
          );
        }
        _collectTypes(declaration, topLevel.types);
      } else if (declaration is TopLevelVariableDeclaration) {
        _collectVariables(declaration.variables, topLevel);
        _collectTypes(declaration, topLevel.types);
      } else if (declaration is FunctionTypeAlias) {
        final name = _publicName(declaration.name.lexeme);
        if (name == null) {
          continue;
        }
        topLevel.fields.add('typedef:$name');
        topLevel.types.add(
          'typedef:$name:arity:${declaration.typeParameters?.typeParameters.length ?? 0}',
        );
        topLevel.types.add(
          _callableSignature('typedef', name, declaration.parameters),
        );
        _collectTypes(declaration, topLevel.types);
      } else if (declaration is GenericTypeAlias) {
        final name = _publicName(declaration.name.lexeme);
        if (name == null) {
          continue;
        }
        topLevel.fields.add('typedef:$name');
        topLevel.types.add(
          'typedef:$name:arity:${declaration.typeParameters?.typeParameters.length ?? 0}',
        );
        _collectTypes(declaration, topLevel.types);
      }
    }
    if (!topLevel.isEmpty) {
      _surface('::', libraryUri).merge(topLevel);
    }
  }

  void _collectClass(ClassDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.namePart.typeName.lexeme);
    if (name == null) {
      return;
    }
    final surface = _surface(name, libraryUri);
    surface.types.add('kind:class');
    surface.types.add(
      'arity:${declaration.namePart.typeParameters?.typeParameters.length ?? 0}',
    );
    _collectExtends(declaration.extendsClause?.superclass, surface);
    _collectWith(declaration.withClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectClassMembers(declaration.body.members, surface, name);
    _collectTypes(declaration, surface.types);
  }

  void _collectMixin(MixinDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.name.lexeme);
    if (name == null) {
      return;
    }
    final surface = _surface(name, libraryUri);
    surface.types.add('kind:mixin');
    surface.types.add(
      'arity:${declaration.typeParameters?.typeParameters.length ?? 0}',
    );
    _collectOn(declaration.onClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectClassMembers(declaration.body.members, surface, name);
    _collectTypes(declaration, surface.types);
  }

  void _collectEnum(EnumDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.namePart.typeName.lexeme);
    if (name == null) {
      return;
    }
    final surface = _surface(name, libraryUri);
    surface.types.add('kind:enum');
    surface.types.add(
      'arity:${declaration.namePart.typeParameters?.typeParameters.length ?? 0}',
    );
    for (final constant in declaration.body.constants) {
      final valueName = _publicName(constant.name.lexeme);
      if (valueName != null) {
        surface.fields.add(valueName);
      }
    }
    _collectWith(declaration.withClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectClassMembers(declaration.body.members, surface, name);
    _collectTypes(declaration, surface.types);
  }

  void _collectExtension(ExtensionDeclaration declaration, String libraryUri) {
    final rawName = declaration.name?.lexeme;
    final onClause = declaration.onClause;
    if (onClause == null) {
      return;
    }
    final name =
        _publicName(rawName) ??
        'extension:${_typeSource(onClause.extendedType)}';
    if (rawName != null && rawName.startsWith('_')) {
      return;
    }
    final surface = _surface(name, libraryUri);
    surface.types.add('kind:extension');
    surface.types.add(
      'arity:${declaration.typeParameters?.typeParameters.length ?? 0}',
    );
    surface.types.add('on:${_typeSource(onClause.extendedType)}');
    _collectClassMembers(declaration.body.members, surface, name);
    _collectTypes(declaration, surface.types);
  }

  void _collectClassMembers(
    NodeList<ClassMember> members,
    _ClassSurface surface,
    String className,
  ) {
    for (final member in members) {
      if (member is ConstructorDeclaration) {
        final name = _constructorName(className, member);
        if (name == null) {
          continue;
        }
        surface.methods.add(name);
        surface.methods.add(
          _callableSignature(
            member.factoryKeyword == null ? 'ctor' : 'factory',
            name,
            member.parameters,
          ),
        );
        _collectTypes(member, surface.types);
      } else if (member is MethodDeclaration) {
        final name = _publicName(member.name.lexeme);
        if (name == null) {
          continue;
        }
        if (member.isGetter) {
          surface.fields.add('get:$name');
        } else if (member.isSetter) {
          surface.fields.add('set:$name');
        } else {
          surface.methods.add(name);
          surface.methods.add(
            _callableSignature('method', name, member.parameters),
          );
        }
        _collectTypes(member, surface.types);
      } else if (member is FieldDeclaration) {
        _collectVariables(member.fields, surface);
        _collectTypes(member, surface.types);
      }
    }
  }

  void _collectVariables(
    VariableDeclarationList variables,
    _ClassSurface surface,
  ) {
    for (final variable in variables.variables) {
      final name = _publicName(variable.name.lexeme);
      if (name != null) {
        surface.fields.add(name);
      }
    }
  }

  void _collectExtends(NamedType? type, _ClassSurface surface) {
    if (type == null) {
      return;
    }
    final name = _typeName(type);
    if (name != null) {
      surface.types.add('extends:$name');
    }
  }

  void _collectWith(WithClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.mixinTypes) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add('mixes:$name');
      }
    }
  }

  void _collectImplements(ImplementsClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.interfaces) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add('implements:$name');
      }
    }
  }

  void _collectOn(MixinOnClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.superclassConstraints) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add('on:$name');
      }
    }
  }

  List<String> _partPaths(CompilationUnit unit, String ownerPath) {
    final result = <String>[];
    for (final directive in unit.directives.whereType<PartDirective>()) {
      final path = _localUriPath(directive.uri.stringValue, ownerPath);
      if (path != null) {
        result.add(path);
      }
    }
    return result..sort();
  }

  String? _localUriPath(String? uri, String ownerPath) {
    if (uri == null || uri.isEmpty || uri.startsWith('dart:')) {
      return null;
    }
    if (uri.startsWith('package:')) {
      final prefix = 'package:$package/';
      if (!uri.startsWith(prefix)) {
        return null;
      }
      return _normalizePath(uri.substring(prefix.length));
    }
    final baseParts = ownerPath.split('/')..removeLast();
    for (final part in uri.split('/')) {
      if (part == '..') {
        if (baseParts.isNotEmpty) {
          baseParts.removeLast();
        }
      } else if (part != '.' && part.isNotEmpty) {
        baseParts.add(part);
      }
    }
    final path = _normalizePath(baseParts.join('/'));
    return File(_join([libDir.path, path])).existsSync() ? path : null;
  }

  _ClassSurface _surface(String name, String libraryUri) {
    return _classes.putIfAbsent(name, () => _ClassSurface())
      ..libraries.add(libraryUri);
  }

  void _merge(Map<String, _ClassSurface> surfaces) {
    for (final entry in surfaces.entries) {
      final target = _classes.putIfAbsent(entry.key, () => _ClassSurface());
      target.merge(entry.value);
    }
  }

  String _relativePath(File file) {
    return _normalizePath(file.path.substring(libDir.path.length + 1));
  }

  String _libraryUri(String relativePath) =>
      'package:$package/${_normalizePath(relativePath)}';
}

class _ApiSurface {
  _ApiSurface(this.classes);

  final Map<String, _ClassSurface> classes;

  void reexportAs(
    String libraryUri, {
    required Set<String> showNames,
    required Set<String> hideNames,
  }) {
    for (final entry in classes.entries) {
      if (!_isExported(entry.key, showNames, hideNames)) {
        continue;
      }
      entry.value.libraries.add(libraryUri);
    }
    final topLevel = classes['::'];
    if (topLevel != null) {
      topLevel.filterTopLevel(showNames: showNames, hideNames: hideNames);
      if (!topLevel.isEmpty) {
        topLevel.libraries.add(libraryUri);
      }
    }
  }

  Map<String, Object?> toJson() {
    final sorted = classes.entries.toList()
      ..sort((a, b) => a.key.compareTo(b.key));
    return {
      'classes': {
        for (final entry in sorted)
          if (!entry.value.isEmpty) entry.key: entry.value.toJson(),
      },
    };
  }

  bool _isExported(String name, Set<String> showNames, Set<String> hideNames) {
    if (name == '::') {
      return true;
    }
    if (showNames.isNotEmpty && !showNames.contains(name)) {
      return false;
    }
    return !hideNames.contains(name);
  }
}

class _ClassSurface {
  final Set<String> libraries = <String>{};
  final Set<String> methods = <String>{};
  final Set<String> fields = <String>{};
  final Set<String> types = <String>{};

  bool get isEmpty => methods.isEmpty && fields.isEmpty && types.isEmpty;

  void merge(_ClassSurface other) {
    libraries.addAll(other.libraries);
    methods.addAll(other.methods);
    fields.addAll(other.fields);
    types.addAll(other.types);
  }

  void filterTopLevel({
    required Set<String> showNames,
    required Set<String> hideNames,
  }) {
    if (showNames.isEmpty && hideNames.isEmpty) {
      return;
    }
    methods.removeWhere(
      (token) => !_topLevelTokenExported(token, showNames, hideNames),
    );
    fields.removeWhere(
      (token) => !_topLevelTokenExported(token, showNames, hideNames),
    );
    types.removeWhere(
      (token) =>
          token.startsWith('typedef:') &&
          !_topLevelTokenExported(token, showNames, hideNames),
    );
  }

  Map<String, Object?> toJson() {
    return {
      'libraries': _sorted(libraries),
      'methods': _sorted(methods),
      'fields': _sorted(fields),
      'types': _sorted(types),
    };
  }
}

class _TypeCollector extends RecursiveAstVisitor<void> {
  _TypeCollector(this.types);

  final Set<String> types;

  @override
  void visitNamedType(NamedType node) {
    final name = _typeName(node);
    if (name != null && !_isBuiltinType(name)) {
      types.add(name);
    }
    super.visitNamedType(node);
  }

  @override
  void visitAnnotation(Annotation node) {
    final name = _publicName(node.name.name);
    if (name != null) {
      types.add('annotation:$name');
    }
    super.visitAnnotation(node);
  }
}

class _Options {
  _Options({required this.packageDir, required this.package});

  final Directory packageDir;
  final String package;

  static _Options parse(List<String> args) {
    final packageDir = _option(args, '--package-dir');
    final package = _option(args, '--package');
    if (packageDir == null || packageDir.isEmpty) {
      throw FormatException('--package-dir is required');
    }
    if (package == null || package.isEmpty) {
      throw FormatException('--package is required');
    }
    return _Options(packageDir: Directory(packageDir), package: package);
  }

  static String? _option(List<String> args, String name) {
    final index = args.indexOf(name);
    if (index < 0 || index + 1 >= args.length) {
      return null;
    }
    return args[index + 1];
  }
}

void _collectTypes(AstNode node, Set<String> types) {
  node.accept(_TypeCollector(types));
}

String? _constructorName(String className, ConstructorDeclaration declaration) {
  if (declaration.name == null) {
    return className;
  }
  final suffix = _publicName(declaration.name!.lexeme);
  if (suffix == null) {
    return null;
  }
  return '$className.$suffix';
}

String _callableSignature(
  String kind,
  String name,
  FormalParameterList? parameters,
) {
  if (parameters == null) {
    return 'sig:$kind:$name()';
  }
  final tokens = <String>[];
  for (final parameter in parameters.parameters) {
    tokens.add(_parameterToken(parameter));
  }
  return 'sig:$kind:$name(${tokens.join(',')})';
}

String _parameterToken(FormalParameter parameter) {
  final required = parameter.isRequiredNamed || parameter.isRequiredPositional;
  final named = parameter.isNamed;
  final positional = parameter.isPositional;
  final hasDefault = parameter.defaultClause != null;
  final name = parameter.name?.lexeme ?? '_';
  final type = parameter.type?.toSource() ?? 'dynamic';
  final shape = named
      ? 'named'
      : positional
      ? 'pos'
      : 'param';
  final req = required ? 'req' : 'opt';
  final def = hasDefault ? '=default' : '';
  return '$shape:$req:$name:$type$def';
}

String? _publicName(String? value) {
  if (value == null || value.isEmpty || value.startsWith('_')) {
    return null;
  }
  return value;
}

String? _typeName(NamedType type) {
  final name = type.name.lexeme;
  if (name.isEmpty) {
    return null;
  }
  return name;
}

String _typeSource(TypeAnnotation type) =>
    type.toSource().replaceAll(RegExp(r'\s+'), ' ');

bool _isPartFile(CompilationUnit unit) {
  return unit.directives.any((directive) => directive is PartOfDirective);
}

bool _topLevelTokenExported(
  String token,
  Set<String> showNames,
  Set<String> hideNames,
) {
  final name = _topLevelExportName(token);
  if (name == null) {
    return true;
  }
  if (showNames.isNotEmpty && !showNames.contains(name)) {
    return false;
  }
  return !hideNames.contains(name);
}

String? _topLevelExportName(String token) {
  if (token.startsWith('sig:')) {
    final rest = token.substring(4);
    final secondColon = rest.indexOf(':');
    if (secondColon < 0) {
      return null;
    }
    final withParams = rest.substring(secondColon + 1);
    final paren = withParams.indexOf('(');
    return paren < 0 ? withParams : withParams.substring(0, paren);
  }
  if (token.startsWith('typedef:')) {
    final name = token.substring('typedef:'.length);
    final colon = name.indexOf(':');
    return colon < 0 ? name : name.substring(0, colon);
  }
  if (token.startsWith('get:') || token.startsWith('set:')) {
    return token.substring(4);
  }
  return token;
}

bool _isBuiltinType(String name) {
  return const {
    'Object',
    'Never',
    'Null',
    'bool',
    'double',
    'dynamic',
    'int',
    'num',
    'String',
    'void',
  }.contains(name);
}

String _normalizePath(String path) =>
    path.replaceAll(Platform.pathSeparator, '/');

String _join(List<String> parts) => parts.join(Platform.pathSeparator);

List<String> _sorted(Set<String> values) => values.toList()..sort();

int _compareUnitEntry(
  MapEntry<String, CompilationUnit> a,
  MapEntry<String, CompilationUnit> b,
) {
  return a.key.compareTo(b.key);
}
