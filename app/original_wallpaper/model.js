/* Pure signal history. Nothing is synthesized when observations stop. */
(function(root) {
  'use strict';
  class TerrainHistory {
    constructor(seconds=48) { this.seconds=seconds; this.clear(); }
    clear() { this.rows=[]; this.sequence=-1; this.generation=null; this.stream=null; this.frequency=null; this.latest=0; }
    ingest(state) {
      const f=state && state.frame;
      if (!f || !Array.isArray(f.levels) || f.levels.length<2 ||
          f.levels.some(x=>!Number.isFinite(x)||x<0||x>1) ||
          !Number.isFinite(f.timestamp) || !Number.isFinite(f.frequency_hz) ||
          !Number.isFinite(f.low_hz) || !Number.isFinite(f.high_hz) || f.high_hz<=f.low_hz) return false;
      const changed=this.generation!==state.generation || this.stream!==state.stream_id || this.frequency!==f.frequency_hz;
      if (changed) this.clear();
      if (state.sequence<=this.sequence) return false;
      // A long missing interval is not a continuous observation.
      if (this.rows.length && (f.timestamp<=this.latest || f.timestamp-this.latest>2)) this.clear();
      this.generation=state.generation; this.stream=state.stream_id; this.frequency=f.frequency_hz; this.sequence=state.sequence;
      this.latest=f.timestamp;
      this.rows.push({time:f.timestamp,levels:Float32Array.from(f.levels),low:f.low_hz,high:f.high_hz});
      while(this.rows.length>256 || (this.rows.length && this.latest-this.rows[0].time>this.seconds)) this.rows.shift();
      return true;
    }
    get duration() { return this.rows.length ? Math.max(0,this.latest-this.rows[0].time) : 0; }
  }
  if(typeof module!=='undefined' && module.exports) module.exports={TerrainHistory};
  else root.TerrainHistory=TerrainHistory;
})(typeof window!=='undefined'?window:this);
