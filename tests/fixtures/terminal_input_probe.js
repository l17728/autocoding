// Real console input fixture: no network, provider, or user data.
process.stdin.setRawMode(true);
process.stdin.resume();
process.stdout.write('READY\n');
process.stdin.on('data', data => {
  if(process.env.AUTOCODING_WIRE_TRACE){
    require('fs').appendFileSync(process.env.AUTOCODING_WIRE_TRACE,data);
    process.stdout.write('RECEIVED:'+data.length+'\n');
  }else process.stdout.write('WIRE:' + data.toString('hex') + '\n');
});
