// Copy selected public registry cache artifacts into a writable offline cache.
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const cacache=require('/opt/homebrew/lib/node_modules/npm/node_modules/cacache');
const [source,destination,...packages]=process.argv.slice(2);
for(const [key,entry] of Object.entries(await cacache.ls(source))){
  if(!packages.some(name=>key.includes(`/${name}/-/`)||key.endsWith(`/${name}`)||key.endsWith(`/${name.replace('/','%2f')}`)))continue;
  try{const value=await cacache.get(source,key);await cacache.put(destination,key,value.data,{metadata:entry.metadata});}catch{/* Ignore incomplete cache records. */}
}
