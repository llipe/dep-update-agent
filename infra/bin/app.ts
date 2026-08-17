#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { TriggerStack } from '../lib/trigger-stack';

const app = new App();

// Configuration — sourced from environment variables or CDK context.
// Set via: export AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:...
// Or:      npx cdk deploy -c agentRuntimeArn=arn:aws:bedrock-agentcore:...
const AGENT_RUNTIME_ARN = app.node.tryGetContext('agentRuntimeArn')
  || process.env.AGENT_RUNTIME_ARN;

if (!AGENT_RUNTIME_ARN) {
  throw new Error(
    'AGENT_RUNTIME_ARN is required. Set it via environment variable or CDK context (-c agentRuntimeArn=...)'
  );
}

const REPOS: string[] = app.node.tryGetContext('repos')
  || JSON.parse(process.env.REPOS || '[]');

if (REPOS.length === 0) {
  throw new Error(
    'REPOS is required. Set it via environment variable (JSON array) or CDK context (-c repos=...)'
  );
}

new TriggerStack(app, 'DepUpdateAgentTrigger', {
  agentRuntimeArn: AGENT_RUNTIME_ARN,
  repos: REPOS,
  allowFixes: (process.env.ALLOW_FIXES || 'true').toLowerCase() === 'true',
  maxFixAttempts: parseInt(process.env.MAX_FIX_ATTEMPTS || '3', 10),
  scheduleExpression: process.env.SCHEDULE_EXPRESSION || 'cron(0 13 ? * MON *)',
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
  },
});

app.synth();
