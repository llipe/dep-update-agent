import { Duration, Stack, type StackProps, CfnOutput } from 'aws-cdk-lib';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import * as path from 'path';

export interface TriggerStackProps extends StackProps {
  /**
   * ARN of the AgentCore runtime to invoke.
   */
  agentRuntimeArn: string;

  /**
   * List of repository URLs to update on each scheduled run.
   */
  repos: string[];

  /**
   * Whether the agent should attempt LLM-powered fixes when tests fail.
   * @default true
   */
  allowFixes?: boolean;

  /**
   * Maximum number of fix attempts per repo.
   * @default 3
   */
  maxFixAttempts?: number;

  /**
   * EventBridge schedule expression.
   * @default "cron(0 13 ? * MON *)" — every Monday at 13:00 UTC
   */
  scheduleExpression?: string;
}

export class TriggerStack extends Stack {
  public readonly triggerFunction: lambda.Function;
  public readonly scheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: TriggerStackProps) {
    super(scope, id, props);

    const {
      agentRuntimeArn,
      repos,
      allowFixes = true,
      maxFixAttempts = 3,
      scheduleExpression = 'cron(0 13 ? * MON *)',
    } = props;

    // Lambda function
    this.triggerFunction = new lambda.Function(this, 'DepUpdateTrigger', {
      functionName: 'dep-agent-trigger',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'trigger')),
      timeout: Duration.minutes(15),
      memorySize: 256,
      environment: {
        AGENT_RUNTIME_ARN: agentRuntimeArn,
        REPOS: JSON.stringify(repos),
        ALLOW_FIXES: String(allowFixes),
        MAX_FIX_ATTEMPTS: String(maxFixAttempts),
      },
      description: 'Triggers dep-update-agent for each configured repository on a schedule.',
    });

    // IAM: allow the Lambda to invoke the AgentCore runtime
    this.triggerFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [agentRuntimeArn, `${agentRuntimeArn}/*`],
      })
    );

    // EventBridge schedule rule
    this.scheduleRule = new events.Rule(this, 'WeeklySchedule', {
      ruleName: 'dep-update-weekly',
      description: 'Triggers dependency update agent every Monday at 13:00 UTC',
      schedule: events.Schedule.expression(scheduleExpression),
      enabled: true,
    });

    this.scheduleRule.addTarget(new targets.LambdaFunction(this.triggerFunction));

    // Outputs
    new CfnOutput(this, 'TriggerFunctionArn', {
      description: 'ARN of the trigger Lambda function',
      value: this.triggerFunction.functionArn,
    });

    new CfnOutput(this, 'ScheduleRuleName', {
      description: 'EventBridge rule name',
      value: this.scheduleRule.ruleName,
    });
  }
}
