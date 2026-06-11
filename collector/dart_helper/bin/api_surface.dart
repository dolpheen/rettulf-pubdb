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
        } else {
          topLevel.methods.add(name);
        }
        _collectTypeAnnotation(declaration.returnType, topLevel.types);
        _collectCallableTypes(
          declaration.functionExpression.parameters,
          topLevel.types,
        );
      } else if (declaration is TopLevelVariableDeclaration) {
        _collectVariables(declaration.variables, topLevel);
        _collectTypeAnnotation(declaration.variables.type, topLevel.types);
      } else if (declaration is FunctionTypeAlias) {
        final name = _publicName(declaration.name.lexeme);
        if (name == null) {
          continue;
        }
        _collectTypeParameters(declaration.typeParameters, topLevel.types);
        _collectTypeAnnotation(declaration.returnType, topLevel.types);
        _collectCallableTypes(declaration.parameters, topLevel.types);
      } else if (declaration is GenericTypeAlias) {
        final name = _publicName(declaration.name.lexeme);
        if (name == null) {
          continue;
        }
        _collectTypeParameters(declaration.typeParameters, topLevel.types);
        _collectTypeAnnotation(declaration.type, topLevel.types);
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
    _collectExtends(declaration.extendsClause?.superclass, surface);
    _collectWith(declaration.withClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectTypeParameters(declaration.namePart.typeParameters, surface.types);
    _collectClassMembers(declaration.body.members, surface, name);
  }

  void _collectMixin(MixinDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.name.lexeme);
    if (name == null) {
      return;
    }
    final surface = _surface(name, libraryUri);
    _collectOn(declaration.onClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectTypeParameters(declaration.typeParameters, surface.types);
    _collectClassMembers(declaration.body.members, surface, name);
  }

  void _collectEnum(EnumDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.namePart.typeName.lexeme);
    if (name == null) {
      return;
    }
    final surface = _surface(name, libraryUri);
    for (final constant in declaration.body.constants) {
      final valueName = _publicName(constant.name.lexeme);
      if (valueName != null) {
        surface.fields.add(valueName);
      }
    }
    _collectWith(declaration.withClause, surface);
    _collectImplements(declaration.implementsClause, surface);
    _collectTypeParameters(declaration.namePart.typeParameters, surface.types);
    _collectClassMembers(declaration.body.members, surface, name);
  }

  void _collectExtension(ExtensionDeclaration declaration, String libraryUri) {
    final rawName = declaration.name?.lexeme;
    final onClause = declaration.onClause;
    if (onClause == null) {
      return;
    }
    if (rawName != null && rawName.startsWith('_')) {
      return;
    }
    final name = _publicName(rawName) ?? '::';
    final surface = _surface(name, libraryUri);
    _collectTypeParameters(declaration.typeParameters, surface.types);
    _collectTypeAnnotation(onClause.extendedType, surface.types);
    _collectClassMembers(declaration.body.members, surface, name);
  }

  void _collectClassMembers(
    NodeList<ClassMember> members,
    _ClassSurface surface,
    String className,
  ) {
    final fieldTypes = _fieldTypes(members);
    for (final member in members) {
      if (member is ConstructorDeclaration) {
        final name = _constructorName(className, member);
        if (name == null) {
          continue;
        }
        surface.methods.add(name);
        _collectCallableTypes(
          member.parameters,
          surface.types,
          fieldTypes: fieldTypes,
        );
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
        }
        _collectTypeAnnotation(member.returnType, surface.types);
        _collectCallableTypes(member.parameters, surface.types);
      } else if (member is FieldDeclaration) {
        _collectVariables(member.fields, surface);
        _collectTypeAnnotation(member.fields.type, surface.types);
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
      surface.types.add(name);
    }
    _collectTypeAnnotation(type, surface.types);
  }

  void _collectWith(WithClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.mixinTypes) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add(name);
      }
      _collectTypeAnnotation(type, surface.types);
    }
  }

  void _collectImplements(ImplementsClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.interfaces) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add(name);
      }
      _collectTypeAnnotation(type, surface.types);
    }
  }

  void _collectOn(MixinOnClause? clause, _ClassSurface surface) {
    if (clause == null) {
      return;
    }
    for (final type in clause.superclassConstraints) {
      final name = _typeName(type);
      if (name != null) {
        surface.types.add(name);
      }
      _collectTypeAnnotation(type, surface.types);
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
      final path = _normalizePath(uri.substring(prefix.length));
      return _libFileExists(path) ? path : null;
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
    return _libFileExists(path) ? path : null;
  }

  bool _libFileExists(String relativePath) {
    return File(_join([libDir.path, relativePath])).existsSync();
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
    final classNames = classes.keys.toSet();
    return {
      'classes': {
        for (final entry in sorted)
          if (entry.key != '::' || !entry.value.isEmpty)
            entry.key: entry.value.toJson(classNames),
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
  }

  Map<String, Object?> toJson(Set<String> classNames) {
    return {
      'libraries': _sorted(libraries),
      'methods': _sorted(methods),
      'fields': _sorted(fields),
      'types': _sorted(types.where(classNames.contains).toSet()),
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

String? _constructorName(String className, ConstructorDeclaration declaration) {
  if (declaration.name == null) {
    return className;
  }
  return _publicName(declaration.name!.lexeme);
}

Map<String, String> _fieldTypes(NodeList<ClassMember> members) {
  final types = <String, String>{};
  for (final member in members.whereType<FieldDeclaration>()) {
    final type = member.fields.type?.toSource();
    if (type == null) {
      continue;
    }
    for (final variable in member.fields.variables) {
      final name = _publicName(variable.name.lexeme);
      if (name != null) {
        types[name] = type;
      }
    }
  }
  return types;
}

void _collectCallableTypes(
  FormalParameterList? parameters,
  Set<String> types, {
  Map<String, String> fieldTypes = const <String, String>{},
}) {
  if (parameters == null) {
    return;
  }
  for (final parameter in parameters.parameters) {
    final explicit = parameter.type;
    if (explicit != null) {
      _collectTypeAnnotation(explicit, types);
      continue;
    }
    if (parameter is FieldFormalParameter) {
      _collectTypeName(fieldTypes[parameter.name.lexeme], types);
    }
  }
}

void _collectTypeParameters(TypeParameterList? parameters, Set<String> types) {
  if (parameters == null) {
    return;
  }
  for (final parameter in parameters.typeParameters) {
    _collectTypeAnnotation(parameter.bound, types);
  }
}

void _collectTypeAnnotation(TypeAnnotation? annotation, Set<String> types) {
  if (annotation == null) {
    return;
  }
  annotation.accept(_TypeCollector(types));
}

void _collectTypeName(String? name, Set<String> types) {
  final normalized = _baseTypeName(name);
  if (normalized != null && !_isBuiltinType(normalized)) {
    types.add(normalized);
  }
}

String? _publicName(String? value) {
  if (value == null || value.isEmpty || value.startsWith('_')) {
    return null;
  }
  return value;
}

String? _typeName(NamedType type) {
  return _baseTypeName(type.name.lexeme);
}

String? _baseTypeName(String? value) {
  if (value == null) {
    return null;
  }
  final name = value.split('<').first.replaceFirst('?', '').trim();
  return name.isEmpty ? null : name;
}

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
    'void',
    'double',
    'dynamic',
    'int',
    'num',
    'String',
  }.contains(name);
}

bool _isAppleDouble(File file) =>
    file.uri.pathSegments.last.startsWith('._');

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
