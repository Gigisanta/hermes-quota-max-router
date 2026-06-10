import os, sys
sys.path.insert(0, '/Users/prueba/workspaces/hermes-quota-max-router')
os.environ['GEMINI_API_KEY'] = '__REDACTED_GEMINI_KEY__'
# Force live mode explicitly
os.environ['ROUTER_LIVE'] = '1'
import uvicorn
uvicorn.run('server.app:app', host='127.0.0.1', port=8087, log_level='warning')
