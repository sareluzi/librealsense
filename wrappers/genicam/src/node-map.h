// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace rs2_gentl {

struct feature_options
{
    std::string selector;  // enumeration whose integer value indexes this feature
    int count = 1;         // number of selector values
    bool standard = true;  // SFNC name, or vendor-specific
    std::string tooltip;
    std::string unit;
};

// A GenApi feature description plus the virtual register space behind it. The XML is rendered from the same
// definitions that serve register reads and writes, so the two can't drift.
class node_map
{
public:
    using int_getter = std::function< int64_t( int index ) >;
    using int_setter = std::function< void( int index, int64_t value ) >;
    using float_getter = std::function< double( int index ) >;
    using float_setter = std::function< void( int index, double value ) >;
    using enum_entries = std::vector< std::pair< std::string, int64_t > >;

    using options = feature_options;

    node_map( std::string port_name, std::string model_name, std::string product_guid );

    void add_category( const std::string & name, const std::vector< std::string > & features, bool standard = true );
    void add_string( const std::string & name, std::function< std::string() > get, const options & opt = {} );
    void add_integer( const std::string & name, int_getter get, int_setter set, const options & opt = {},
                      int_getter min = nullptr, int_getter max = nullptr );
    void add_float( const std::string & name, float_getter get, float_setter set, const options & opt = {},
                    float_getter min = nullptr, float_getter max = nullptr );
    void add_boolean( const std::string & name, int_getter get, int_setter set, const options & opt = {} );
    void add_enumeration( const std::string & name, const enum_entries & entries, int_getter get, int_setter set,
                          const options & opt = {} );
    void add_command( const std::string & name, std::function< void() > execute, const options & opt = {} );

    // Renders the XML; no features may be added afterwards
    void finalize();

    const std::string & port_name() const { return _port_name; }
    const std::string & xml() const { return _xml; }
    std::string xml_file_name() const { return _model_name + ".xml"; }
    uint64_t xml_address() const { return XML_ADDRESS; }
    std::string url() const;

    void read( uint64_t address, void * buffer, size_t size ) const;
    void write( uint64_t address, const void * buffer, size_t size );

private:
    struct reg
    {
        uint64_t address;
        size_t length;
        int count;
        std::function< void( int index, uint8_t * out ) > read;
        std::function< void( int index, const uint8_t * in ) > write;  // empty for read-only registers
    };

    static const uint64_t XML_ADDRESS = 0x10000000;
    static const uint64_t REGISTER_STRIDE = 0x100;

    std::string add_int_register( const std::string & name, int_getter get, int_setter set, const options & opt );
    std::string add_float_register( const std::string & name, float_getter get, float_setter set, const options & opt );
    uint64_t add_register( size_t length, const options & opt, std::function< void( int, uint8_t * ) > read,
                           std::function< void( int, const uint8_t * ) > write );
    void register_node( const char * type, const std::string & name, uint64_t address, size_t length, bool writable,
                        const options & opt, const char * tail );
    void open_feature( const char * type, const std::string & name, const options & opt );
    const reg & find( uint64_t address, int & index, size_t & offset ) const;

    std::string _port_name;
    std::string _model_name;
    std::string _product_guid;
    std::vector< reg > _regs;
    std::ostringstream _nodes;
    std::map< std::string, std::vector< std::string > > _selected;  // selector -> features it indexes
    std::string _xml;
};

}  // namespace rs2_gentl
