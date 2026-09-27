/**
 * Order Tracker - MCP Deploy
 *
 * Exposes the VM's deploy webhook (deploy/deploy.py) as two MCP tools, so
 * Claude (or any other MCP client wired to this n8n instance) can redeploy
 * the order tracker just by being asked, instead of you running commands:
 *
 *   - "Redeploy Order Tracker"        -> POST /deploy         (git pull + docker compose up -d --build)
 *   - "Order Tracker Deploy Status"   -> GET  /deploy/status   (state, exit code, log tail, git commit)
 *
 * This is n8n Workflow SDK code (validated with n8n's validate_workflow tool),
 * not a workflow JSON export - your n8n MCP tools here could validate it but
 * not create/deploy the workflow itself. To turn it into a running workflow:
 *
 *   - if your n8n has the AI Workflow Builder (chat-based workflow creation),
 *     paste this file's contents there and ask it to create the workflow from
 *     this code; OR
 *   - recreate the 3 nodes by hand using the shapes below: an "MCP Server
 *     Trigger" node (Bearer auth) with two "HTTP Request" tool subnodes
 *     wired to it (Method/URL/auth exactly as configured here).
 *
 * Before running it:
 *   1. Replace YOUR-VM-HOST in both tool URLs with the public hostname from
 *      your Caddyfile (e.g. tracker.yourdomain.com). n8n runs as a container
 *      in the same stack as Caddy, but it reaches this over the open
 *      internet like any other client - no special network access needed,
 *      since Caddy is already the public entry point for /deploy too.
 *   2. Create the "Order Tracker Deploy Token" credential (HTTP Bearer Auth)
 *      with the same value as DEPLOY_TOKEN in the VM's .env.
 *   3. Create the "Order Tracker MCP Access" credential (HTTP Bearer Auth)
 *      with a token of your choosing - this is what protects the MCP
 *      endpoint itself (n8n shows the endpoint's URL on the trigger node
 *      once saved); give that URL + token to whatever MCP client will call it.
 *   4. Activate the workflow, then connect to it as an MCP server from
 *      Claude Code / Claude Desktop / another MCP client.
 */
import { workflow, trigger, tool, sticky, placeholder, newCredential } from '@n8n/workflow-sdk';

const redeployTool = tool({
  type: 'n8n-nodes-base.httpRequestTool',
  version: 4.5,
  config: {
    name: 'Redeploy Order Tracker',
    parameters: {
      method: 'POST',
      url: placeholder('https://YOUR-VM-HOST/deploy'),
      authentication: 'genericCredentialType',
      genericAuthType: 'httpBearerAuth',
      sendBody: false
    },
    credentials: { httpBearerAuth: newCredential('Order Tracker Deploy Token') },
    output: [{ status: 'started' }]
  }
});

const deployStatusTool = tool({
  type: 'n8n-nodes-base.httpRequestTool',
  version: 4.5,
  config: {
    name: 'Order Tracker Deploy Status',
    parameters: {
      method: 'GET',
      url: placeholder('https://YOUR-VM-HOST/deploy/status'),
      authentication: 'genericCredentialType',
      genericAuthType: 'httpBearerAuth'
    },
    credentials: { httpBearerAuth: newCredential('Order Tracker Deploy Token') },
    output: [{ state: 'idle', commit: 'abc1234' }]
  }
});

const mcpEntry = trigger({
  type: '@n8n/n8n-nodes-langchain.mcpTrigger',
  version: 2.1,
  config: {
    name: 'Order Tracker MCP',
    parameters: {
      path: 'order-tracker',
      authentication: 'bearerAuth',
      instructions:
        'Tools for the ISP Order Tracker on the Debian VM. Use "Redeploy Order Tracker" after code ' +
        'has been pushed/edited on the VM, to git-pull and rebuild the Docker container. Use "Order ' +
        'Tracker Deploy Status" to check whether a deploy is still running and see its outcome before ' +
        'reporting back to the user.'
    },
    credentials: { httpBearerAuth: newCredential('Order Tracker MCP Access') },
    subnodes: { tools: [redeployTool, deployStatusTool] },
    output: [{}]
  }
});

const note = sticky(
  '## Order Tracker CI/CD\n' +
    'Backed by deploy/deploy.py on the VM:\n' +
    '- Redeploy Order Tracker -> POST /deploy\n' +
    '- Order Tracker Deploy Status -> GET /deploy/status\n\n' +
    'Fill in the VM host in both HTTP Request Tool URLs, and set the "Order Tracker Deploy Token" ' +
    'credential to the same value as DEPLOY_TOKEN in .env on the VM.',
  [mcpEntry],
  { color: 4 }
);

export default workflow('order-tracker-mcp-deploy', 'Order Tracker - MCP Deploy')
  .add(mcpEntry)
  .add(note);
