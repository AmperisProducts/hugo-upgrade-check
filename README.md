# hugo-upgrade-check

Know what a new [Hugo](https://gohugo.io/) release changes in your site before you upgrade.

`hugo-upgrade-check` builds your project twice, once with the Hugo version you use now and once with a newer release, and compares the two outputs. It reports the differences that matter for a published site and ignores the build noise that changes on every release, such as the generator tag, fingerprints, integrity attributes and the hashes in the names of processed images.

It runs as a GitHub Action or as a single Python script on Linux, macOS and Windows. The script uses only the Python standard library. Pillow is optional and enables the visual image check.

## What it checks

- **Published files**: files that appear or disappear. Processed images and other generated files whose names change only because their content hash changed are matched, not reported as removed and added.
- **SEO elements on every HTML page**: title, meta description, canonical, `og:url`, robots, `hreflang` alternates and alias redirects.
- **HTML content**: pages whose HTML changes after removing build noise.
- **Other files**: CSS, JavaScript, XML, JSON and other files with different content, by extension.
- **Images**: images with different bytes, and whether they look different. An image counts as different when its size changes or its PSNR is below 35 dB (requires Pillow).
- **Sitemap**: URLs added to or removed from `sitemap*.xml`.
- **Build log**: WARN and ERROR lines that appear only with the new version.
- **Release notes** of every release in between: security fixes, announced breaking changes, and identifiers mentioned as deprecated or removed that still appear in your layouts, assets or configuration.
- **Pinned versions**: the files and lines where your project pins the Hugo version (`HUGO_VERSION` in CI files, `netlify.toml`, `build.sh`, `.hugo-version`, `module.hugoVersion.min`), so you know what to update.

The result is one of `identical`, `differences`, `build-failed` or `up-to-date`, with a Markdown report, a JSON report and a CSV file listing every difference.

## Use it in GitHub Actions

This workflow checks every Monday whether the latest Hugo release changes your site. The report appears in the job summary and as an artifact.

```yaml
name: Hugo upgrade check

on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0 # needed if your project uses enableGitInfo
          submodules: recursive # needed if your theme is a Git submodule

      # Install whatever your build needs besides Hugo, for example Dart Sass, Node.js or Go.

      - name: Compare the current and the latest Hugo release
        id: hugo
        uses: AmperisProducts/hugo-upgrade-check@v1
        with:
          build-args: --minify --gc

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: hugo-upgrade-check
          path: hugo-upgrade-check/
```

Use the `result` output to decide what happens next. For example, open a pull request that bumps the version when the result is `identical`, or open an issue when it is `differences`.

### Inputs

Name | Default | Description
:-- | :-- | :--
`source` | `.` | Project directory.
`current` | detected | Hugo version the project uses now. When empty, it is read from the `HUGO_VERSION` environment variable, then from the files where the project pins the version.
`target` | `latest` | Hugo version to test, or `latest` for the latest stable release.
`build-args` | empty | Extra arguments for `hugo build`. Use the same arguments as your production build.
`output` | `hugo-upgrade-check` | Directory for the reports and the build logs.
`fail-on` | `failure` | Fail the step on `failure` (a build fails), `differences` (any difference) or `never`.
`max-html-changed` | `0` | Fraction of HTML pages, from 0 to 1, allowed to change before the result is `differences`.
`extended` | `true` | Use the extended edition for Hugo versions before 0.170.0, the release that dropped it.
`github-token` | `github.token` | Token for the GitHub API, used to read release notes without hitting rate limits.

### Outputs

Name | Description
:-- | :--
`result` | `up-to-date`, `identical`, `differences` or `build-failed`.
`current` | Hugo version the project uses now.
`target` | Hugo version tested.
`update-available` | `true` when the target version is newer than the current one.
`report` | Path of the Markdown report.

## Use it locally

```sh
python3 hugo_upgrade_check.py --source path/to/project --build-args="--minify --gc"
python3 hugo_upgrade_check.py --current 0.169.0 --target 0.170.1 --keep-builds
```

`--keep-builds` keeps both `public` directories in the output directory, so you can inspect any difference with your own diff tool. Run `python3 hugo_upgrade_check.py --help` for every option. Write `--build-args` with an equals sign, because its value starts with a dash.

## Limitations

- Both builds run with the same command line and environment. Tools that your build needs besides Hugo, such as Dart Sass, PostCSS or Go for Hugo Modules, must already be installed.
- The release notes check is a heuristic. It points to identifiers worth a look; it does not prove that a change affects you.
- Content that depends on the build time, such as `lastmod` from the system clock, or on remote data fetched with `resources.GetRemote`, can differ between the two builds for reasons unrelated to Hugo.

## Origin

Amperis Products S.L. built this tool for the upgrade pipeline of [amperis.com](https://amperis.com), a Hugo site in six languages, and published a generic version for the Hugo community. Issues and pull requests are welcome.

## License

[Apache License 2.0](LICENSE), the same license as Hugo.
