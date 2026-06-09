import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';

Future<void> main(List<String> args) async {
  try {
    final options = _Options.parse(args);
    final manifest = _Collector(options.packageDir, options.package).collect();
    stdout.writeln(jsonEncode(manifest.toJson()));
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
  final _ProbeManifest _manifest = _ProbeManifest();

  _ProbeManifest collect() {
    if (!libDir.existsSync()) {
      return _manifest;
    }

    for (final file in _dartFiles()) {
      _unitFor(_relativePath(file));
    }

    for (final entry in _units.entries.toList()..sort(_compareUnitEntry)) {
      if (_isPartFile(entry.value)) {
        continue;
      }
      _collectLibrary(entry.key, _libraryUri(entry.key), <String>{});
    }
    return _manifest;
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

    _manifest.libraries.add(libraryUri);
    final unit = _unitFor(normalized);
    _collectUnitDeclarations(unit, libraryUri);
    for (final partPath in _partPaths(unit, normalized)) {
      _collectLibrary(partPath, libraryUri, visiting);
    }
    visiting.remove(normalized);
  }

  void _collectUnitDeclarations(CompilationUnit unit, String libraryUri) {
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
        _collectTopLevelFunction(declaration, libraryUri);
      } else if (declaration is TopLevelVariableDeclaration) {
        _collectTopLevelVariables(declaration, libraryUri);
      }
    }
  }

  void _collectClass(ClassDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.namePart.typeName.lexeme);
    if (name == null) {
      return;
    }
    _manifest.add(
      library: libraryUri,
      expression: name,
      declaration: _classDeclaration(name),
      kind: 'type',
    );
    for (final member
        in declaration.body.members.whereType<ConstructorDeclaration>()) {
      if (declaration.abstractKeyword != null &&
          member.factoryKeyword == null) {
        continue;
      }
      final constructorName = _constructorName(name, member);
      if (constructorName == null) {
        continue;
      }
      _manifest.add(
        library: libraryUri,
        expression: constructorName == name
            ? '$name.new'
            : '$name.$constructorName',
        declaration: _memberDeclaration(name, 'method', constructorName),
        kind: 'constructor',
      );
    }
    _collectStaticMembers(declaration.body.members, libraryUri, name);
  }

  void _collectMixin(MixinDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.name.lexeme);
    if (name == null) {
      return;
    }
    _manifest.add(
      library: libraryUri,
      expression: name,
      declaration: _classDeclaration(name),
      kind: 'type',
    );
    _collectStaticMembers(declaration.body.members, libraryUri, name);
  }

  void _collectEnum(EnumDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.namePart.typeName.lexeme);
    if (name == null) {
      return;
    }
    _manifest.add(
      library: libraryUri,
      expression: name,
      declaration: _classDeclaration(name),
      kind: 'type',
    );
    for (final constant in declaration.body.constants) {
      final valueName = _publicName(constant.name.lexeme);
      if (valueName == null) {
        continue;
      }
      _manifest.add(
        library: libraryUri,
        expression: '$name.$valueName',
        declaration: _memberDeclaration(name, 'field', valueName),
        kind: 'enum-value',
      );
    }
    _collectStaticMembers(declaration.body.members, libraryUri, name);
  }

  void _collectExtension(ExtensionDeclaration declaration, String libraryUri) {
    final name = _publicName(declaration.name?.lexeme);
    if (name == null) {
      return;
    }
    _collectStaticMembers(declaration.body.members, libraryUri, name);
  }

  void _collectTopLevelFunction(
    FunctionDeclaration declaration,
    String libraryUri,
  ) {
    final name = _publicName(declaration.name.lexeme);
    if (name == null || declaration.isSetter) {
      return;
    }
    _manifest.add(
      library: libraryUri,
      expression: name,
      declaration: _memberDeclaration(
        '::',
        declaration.isGetter ? 'field' : 'method',
        declaration.isGetter ? 'get:$name' : name,
      ),
      kind: declaration.isGetter ? 'top-level-getter' : 'top-level-function',
    );
  }

  void _collectTopLevelVariables(
    TopLevelVariableDeclaration declaration,
    String libraryUri,
  ) {
    for (final variable in declaration.variables.variables) {
      final name = _publicName(variable.name.lexeme);
      if (name == null) {
        continue;
      }
      _manifest.add(
        library: libraryUri,
        expression: name,
        declaration: _memberDeclaration('::', 'field', name),
        kind: 'top-level-field',
      );
    }
  }

  void _collectStaticMembers(
    NodeList<ClassMember> members,
    String libraryUri,
    String ownerName,
  ) {
    for (final member in members) {
      if (member is MethodDeclaration) {
        if (!member.isStatic || member.isSetter) {
          continue;
        }
        final name = _publicName(member.name.lexeme);
        if (name == null) {
          continue;
        }
        _manifest.add(
          library: libraryUri,
          expression: '$ownerName.$name',
          declaration: _memberDeclaration(
            ownerName,
            member.isGetter ? 'field' : 'method',
            member.isGetter ? 'get:$name' : name,
          ),
          kind: member.isGetter ? 'static-getter' : 'static-method',
        );
      } else if (member is FieldDeclaration) {
        if (!member.isStatic) {
          continue;
        }
        for (final variable in member.fields.variables) {
          final name = _publicName(variable.name.lexeme);
          if (name == null) {
            continue;
          }
          _manifest.add(
            library: libraryUri,
            expression: '$ownerName.$name',
            declaration: _memberDeclaration(ownerName, 'field', name),
            kind: 'static-field',
          );
        }
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

  String _relativePath(File file) {
    return _normalizePath(file.path.substring(libDir.path.length + 1));
  }

  String _libraryUri(String relativePath) =>
      'package:$package/${_normalizePath(relativePath)}';
}

class _ProbeManifest {
  final Set<String> libraries = <String>{};
  final Map<String, _ProbeReference> references = <String, _ProbeReference>{};

  void add({
    required String library,
    required String expression,
    required String declaration,
    required String kind,
  }) {
    references.putIfAbsent(
      '$library::$expression::$declaration',
      () => _ProbeReference(
        library: library,
        expression: expression,
        declaration: declaration,
        kind: kind,
      ),
    );
  }

  Map<String, Object?> toJson() {
    final sortedReferences = references.values.toList()
      ..sort((a, b) {
        final libraryCompare = a.library.compareTo(b.library);
        if (libraryCompare != 0) {
          return libraryCompare;
        }
        return a.expression.compareTo(b.expression);
      });
    return {
      'libraries': _sorted(libraries),
      'references': [
        for (final reference in sortedReferences) reference.toJson(),
      ],
      'total_references': sortedReferences.length,
    };
  }
}

class _ProbeReference {
  const _ProbeReference({
    required this.library,
    required this.expression,
    required this.declaration,
    required this.kind,
  });

  final String library;
  final String expression;
  final String declaration;
  final String kind;

  Map<String, String> toJson() => {
    'library': library,
    'expression': expression,
    'declaration': declaration,
    'kind': kind,
  };
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

String? _publicName(String? value) {
  if (value == null || value.isEmpty || value.startsWith('_')) {
    return null;
  }
  return value;
}

String _classDeclaration(String name) => 'class:$name';

String _memberDeclaration(String owner, String bucket, String name) =>
    'member:$owner:$bucket:$name';

bool _isPartFile(CompilationUnit unit) {
  return unit.directives.any((directive) => directive is PartOfDirective);
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
