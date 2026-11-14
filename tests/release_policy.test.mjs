import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {analyzeCommits} from '@semantic-release/commit-analyzer';
import {generateNotes} from '@semantic-release/release-notes-generator';

const config = JSON.parse(await readFile(new URL('../.releaserc.json', import.meta.url)));
const [, options] = config.plugins.find(([name]) => name === '@semantic-release/commit-analyzer');
const logger = {log() {}};

for (const [message, expected] of [
  ['fix(swarm): preserve installed version on failed update', 'patch'],
  ['feat(releases): add Claude skill bundle', 'minor'],
  ['feat(installer)!: change package layout', 'major'],
  ['fix(installer): reject old packages\n\nBREAKING CHANGE: schema 1 packages are no longer accepted', 'major'],
  ['docs: describe Cloud setup', null],
  ['test: cover bundle installation', null],
  ['chore: update development tooling', null],
]) {
  test(`configured release policy: ${message.split('\n')[0]}`, async () => {
    const actual = await analyzeCommits(options, {commits: [{hash: 'a'.repeat(40), message}], logger});
    assert.equal(actual, expected);
  });
}

test('a mixed main history uses the most significant change', async () => {
  const commits = [
    'docs: add usage notes',
    'feat(bundle): add platform archive',
    'fix(installer): keep previous version',
  ].map((message, i) => ({hash: i.toString(16).repeat(40), message}));
  assert.equal(await analyzeCommits(options, {commits, logger}), 'minor');
});

test('configured notes generator renders a first release and an update', async () => {
  const [, notesOptions] = config.plugins.find(([name]) => name === '@semantic-release/release-notes-generator');
  for (const lastRelease of [{}, {version: '1.0.0', gitTag: 'v1.0.0'}]) {
    const notes = await generateNotes(notesOptions, {
      commits: [{hash: 'a'.repeat(40), message: 'feat(releases): publish skill archives'},
                {hash: 'b'.repeat(40), message: 'fix(releases): preserve package version'}],
      lastRelease, nextRelease: {version: '1.1.0', gitTag: 'v1.1.0'},
      options: {repositoryUrl: 'https://github.com/mv-go/agent-skills.git'},
      cwd: process.cwd(), logger,
    });
    assert.match(notes, /publish skill archives/);
    assert.match(notes, /preserve package version/);
    assert.match(notes, /1\.1\.0/);
  }
});
