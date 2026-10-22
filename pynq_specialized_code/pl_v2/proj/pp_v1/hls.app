<AutoPilot:project xmlns:AutoPilot="com.autoesl.autopilot.project" top="interop_accel" name="pp_v1" ideType="classic">
    <files>
        <file name="../src/interop_accel.cpp" sc="0" tb="false" cflags="-DUSE_HLS_TYPES -I/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl_v2/src" csimflags="" blackbox="false"/>
        <file name="/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl_v2/tb/tb_interop.cpp" sc="0" tb="1" cflags="-DUSE_HLS_TYPES -I/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl_v2/src -Wno-unknown-pragmas" csimflags="" blackbox="false"/>
    </files>
    <solutions>
        <solution name="solution1" status=""/>
    </solutions>
    <Simulation argv="">
        <SimFlow name="csim" setup="false" optimizeCompile="false" clean="true" ldflags="" mflags=""/>
    </Simulation>
</AutoPilot:project>
